"""Real admitted client code, isolated SQLite/evidence, local fake transport only."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
from threading import get_ident
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from test_deepseek_json_observation import observed, native, invoke
from test_deepseek_required import Response
from test_deepseek_strict_clean import execution as strict_execution
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


@pytest.mark.parametrize("retry", [False, True])
def test_new_epoch_replays_settled_actual_native_without_another_send(observed, tmp_path, monkeypatch, retry):
    clock = [BASE]
    store, lease, child = child_fixture(tmp_path, observed, clock=lambda: clock[0])
    replies = ([native('{"score":', "first-native")] if retry else []) + [native('{"score":7,"reason":"ok"}', "last-native")]
    iterator = iter(replies)
    calls = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append(kwargs["data"]) or Response(next(iterator)))
    with pytest.raises(RuntimeError, match="after durable native"):
        with child.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}):
            assert invoke(child) == {"score": 7, "reason": "ok"}
            raise RuntimeError("TEST_FIXTURE process exit after durable native")
    count = len(calls)
    with store._tx() as connection:
        before_observations = store._rows(connection, "observations")
    clock[0] += 61
    store.recover_expired()
    newer = store.claim_ready("new-worker")
    replay = UnitExecutionContext(observed, work_store=store, lease=newer, stage="clean.llm_semantic")
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("replay must never send"))
    with replay.step("clean.llm_semantic", {"TEST_FIXTURE": "whole original"}) as scope:
        scope.output(invoke(replay))
    proof = verify_child_seal(store, replay.seal())
    assert count == (2 if retry else 1) and proof["epoch"] == 2
    assert store.status()["format_reanswers"] == (1 if retry else 0)
    assert store.status()["known_usage"]["total_tokens"] == (100 if retry else 50)
    with store._tx() as connection:
        assert store._rows(connection, "observations") == before_observations
    attempts = store.attempts_for_unit("extract")
    assert replay.model_call_id("new-call-id") == store.read_attempt_request(attempts[0]["attempt_id"])["call_id"]


def test_native_replay_rejects_changed_wire_before_any_new_effect(observed, tmp_path, monkeypatch):
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from test_deepseek_required import SCHEMA
    clock = [BASE]
    store, lease, child = child_fixture(tmp_path, observed, clock=lambda: clock[0])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response(native('{"score":7,"reason":"ok"}', "actual-native")))
    with pytest.raises(RuntimeError):
        with child.step("clean.llm_semantic", {}):
            invoke(child)
            raise RuntimeError("TEST_FIXTURE crash")
    clock[0] += 61
    store.recover_expired()
    replay = UnitExecutionContext(observed, work_store=store, lease=store.claim_ready("new"), stage="clean.llm_semantic")
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("changed request must never send"))
    with pytest.raises(WorkUnitError) as caught:
        with replay.step("clean.llm_semantic", {}):
            DeepSeekRequiredClient(replay).complete_json(system="TEST_FIXTURE JSON", user="DIFFERENT ORIGINAL",
                                                        schema_hint={}, schema=SCHEMA)
    assert caught.value.code == "ATTEMPT_MISMATCH"


def test_rejected_saved_native_stays_rejected_without_claiming_any_new_retry(observed, tmp_path, monkeypatch):
    clock = [BASE]
    store, lease, child = child_fixture(tmp_path, observed, clock=lambda: clock[0])
    replies = iter([native('{"score":', "rejected-first"), native('{"score":', "rejected-second")])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response(next(replies)))
    with pytest.raises(RequiredModelUnavailable) as original:
        with child.step("clean.llm_semantic", {}):
            invoke(child)
    assert original.value.diagnostic["code"] == "INVALID_RESPONSE_JSON"
    clock[0] += 61
    store.recover_expired()
    replay = UnitExecutionContext(observed, work_store=store, lease=store.claim_ready("new"), stage="clean.llm_semantic")
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("rejected native must never resend"))
    with pytest.raises(RequiredModelUnavailable) as restored:
        with replay.step("clean.llm_semantic", {}):
            invoke(replay)
    assert restored.value.diagnostic == original.value.diagnostic
    assert len(restored.value.observation["attempts"]) == 2
    assert store.status()["format_reanswers"] == 1 and store.status()["known_usage"]["total_tokens"] == 100


@pytest.mark.parametrize("retry", [False, True])
def test_original_tool_arguments_are_replayed_losslessly_with_local_http_only(strict_execution, tmp_path, monkeypatch, retry):
    from test_deepseek_strict_clean import complete, native as tool_native, real_http
    parent = strict_execution[0]
    clock = [BASE]
    store = WorkStore.create(tmp_path / "strict-units", manifest(run_id=parent.run_id), clock=lambda: clock[0])
    store.plan([unit("tool")], originals=[])
    child = UnitExecutionContext(parent, work_store=store, lease=store.claim_ready("worker"), stage="clean.llm_semantic")
    arguments = []
    def produce(path, body, number):
        value = tool_native("{bad original arguments" if retry and number == 1 else None,
                            identifier="TEST_FIXTURE_tool_" + str(number))
        arguments.append(value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        return value
    with real_http(monkeypatch, produce) as http:
        with pytest.raises(RuntimeError):
            with child.step("clean.llm_semantic", {}):
                expected = complete(child)
                raise RuntimeError("TEST_FIXTURE crash before result commit")
        assert len(http["wires"]) == (2 if retry else 1)
    clock[0] += 61
    store.recover_expired()
    restored = UnitExecutionContext(parent, work_store=store, lease=store.claim_ready("new"), stage="clean.llm_semantic")
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("saved strict tool native must never resend"))
    with restored.step("clean.llm_semantic", {}) as scope:
        actual = complete(restored)
        scope.output(actual)
    assert actual == expected
    verify_child_seal(store, restored.seal())
    assert store.status()["known_usage"]["total_tokens"] == (400 if retry else 200)
    from trading_system.agent_evidence import read_snapshot
    call = restored.summary()[0]
    stage_output = read_snapshot(restored.child_root, call["output_ref"], run_id=parent.run_id, stage="clean.llm_semantic", role="output")
    proof = read_snapshot(restored.child_root, stage_output["io_calls"][0]["output_ref"], run_id=parent.run_id, stage="clean.llm_semantic", role="output")
    assert proof["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == arguments[-1]


@pytest.mark.parametrize("zero_links", [False, True])
def test_sqlite_sidecar_removed_by_last_connection_close_is_not_an_unsafe_file(tmp_path, monkeypatch, zero_links):
    store = create(tmp_path)
    sidecar = store.root / "work-units.sqlite3-wal"
    sidecar.write_bytes(b"")
    original = Path.lstat
    removed = []
    def disappeared(path, *args, **kwargs):
        if path == sidecar and not removed:
            removed.append(path)
            path.unlink()
            if zero_links:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0)
            raise FileNotFoundError("TEST_FIXTURE SQLite removed its own WAL")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", disappeared)
    assert store.status()["graph_state"] == "ready"
    assert removed == [sidecar]


def test_local_budget_rejection_keeps_native_diagnostic_but_never_creates_provider_unknown(observed, tmp_path):
    from trading_system.llm.json_observation import CONTRACT as OBSERVATION_CONTRACT, ATTEMPT_SCHEMA, staged_diagnostic
    store, lease, child = child_fixture(tmp_path, observed)
    inputs = {"request": {"TEST_FIXTURE": "wire"}, "output_ceiling": 20, "call_id": "c" * 32,
              "request_sha256": "1" * 64, "response_observation_contract": OBSERVATION_CONTRACT}
    transport = {"http_status": None, "body_sha256": None, "body_bytes": None,
                 "body_whitespace_only": None, "body_complete": False}
    error = RequiredModelUnavailable("TRUSTED_CONFIGURATION_MISMATCH")
    native = {"schema": ATTEMPT_SCHEMA, "call_id": inputs["call_id"], "ordinal": 1,
              "request_sha256": inputs["request_sha256"], "transport": transport, "response": None,
              "response_sha256": None, "content_redacted": None, "reasoning_state": None,
              "fingerprint_valid": None, "transport_diagnostic": staged_diagnostic(error, "http_envelope", transport)}
    native_refs = []
    def local_guard():
        native_refs.append(child.model_attempt_snapshot(native))
        raise error
    with pytest.raises(RequiredModelUnavailable):
        with child.step("clean.llm_semantic", {}):
            child.call_io("model.deepseek.chat", inputs, local_guard)
    assert store.status()["unknown_attempts"] == 0
    with store._tx() as connection:
        assert store._rows(connection, "observations") == []
    store.fail(lease, "LOCAL_BUDGET_REJECTED", retryable=False)
    attempt = store.attempts_for_unit("extract")[0]
    assert attempt["state"] == "not_sent" and store.status()["held_reservations"] == []
    from trading_system.agent_evidence import read_snapshot
    assert read_snapshot(child.child_root, native_refs[0], run_id=child.run_id,
                         stage="clean.llm_semantic", role="output") == native


def test_actual_transport_hook_requires_exact_wire_and_dispatches_once(observed, tmp_path):
    store, lease, child = child_fixture(tmp_path, observed)
    inputs = {"request": {"TEST_FIXTURE": "original"}, "output_ceiling": 20}
    def invoke_hook():
        with pytest.raises(WorkUnitError) as changed:
            child.model_transport_dispatch({"TEST_FIXTURE": "changed"})
        assert changed.value.code == "ATTEMPT_MISMATCH" and store.status()["unknown_attempts"] == 0
        child.model_transport_dispatch(inputs["request"])
        assert store.status()["unknown_attempts"] == 1
        with pytest.raises(WorkUnitError):
            child.model_transport_dispatch(inputs["request"])
        raise RequiredModelUnavailable("NETWORK_TIMEOUT")
    with pytest.raises(RequiredModelUnavailable):
        with child.step("clean.llm_semantic", {}):
            child.call_io("model.deepseek.chat", inputs, invoke_hook)
    store.fail(lease, "NETWORK_TIMEOUT", retryable=False)
    assert store.status()["unknown_attempts"] == 1 and store.status()["held_reservations"]


@pytest.mark.parametrize("window", ["before_retry_cas", "after_retry_cas", "after_retry_prepare"])
def test_recovered_original_format_reanswer_resumes_only_its_known_not_sent_second_attempt(observed, tmp_path, monkeypatch, window):
    clock = [BASE]
    store, lease, child = child_fixture(tmp_path, observed, clock=lambda: clock[0])
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs["data"])
        return Response(native('{"score":' if len(calls) == 1 else '{"score":7,"reason":"ok"}',
                               "TEST_FIXTURE_resume_" + str(len(calls))))
    monkeypatch.setattr(requests, "post", post)
    original_claim = child.claim_model_format_retry
    def crash():
        if window == "after_retry_cas":
            assert store.claim_format_retry(child.lease, child._task_attempt.attempt_id,
                eligibility_verifier=lambda request, native: True)
        elif window == "after_retry_prepare":
            assert original_claim() is True
        raise RuntimeError("TEST_FIXTURE crash in reanswer window")
    monkeypatch.setattr(child, "claim_model_format_retry", crash)
    with pytest.raises(RuntimeError, match="reanswer window"):
        with child.step("clean.llm_semantic", {}):
            invoke(child)
    assert len(calls) == 1 and store.status()["settled_attempts"] == 1
    clock[0] += 61
    store.recover_expired()
    restored = UnitExecutionContext(observed, work_store=store, lease=store.claim_ready("resumed"), stage="clean.llm_semantic")
    with restored.step("clean.llm_semantic", {}) as scope:
        result = invoke(restored)
        scope.output(result)
    assert result == {"score": 7, "reason": "ok"} and len(calls) == 2 and calls[0] == calls[1]
    assert store.status()["format_reanswers"] == 1 and store.status()["settled_attempts"] == 2
    assert store.status()["known_usage"]["total_tokens"] == 100
    verify_child_seal(store, restored.seal())


def test_crash_before_first_actual_dispatch_restores_original_call_id_and_sends_once(observed, tmp_path, monkeypatch):
    clock = [BASE]
    store, lease, child = child_fixture(tmp_path, observed, clock=lambda: clock[0])
    sent = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: sent.append(kwargs["data"]) or Response(
        native('{"score":7,"reason":"ok"}', "TEST_FIXTURE_first_actual_send")))
    def crash_before_dispatch(request):
        raise SystemExit(73)
    monkeypatch.setattr(child, "model_transport_dispatch", crash_before_dispatch)
    with pytest.raises(SystemExit) as stopped:
        with child.step("clean.llm_semantic", {}):
            invoke(child)
    assert stopped.value.code == 73
    assert sent == [] and store.attempts_for_unit("extract")[0]["state"] == "prepared"
    original_call_id = store.read_attempt_request(store.attempts_for_unit("extract")[0]["attempt_id"])["call_id"]
    clock[0] += 61
    store.recover_expired()
    restored = UnitExecutionContext(observed, work_store=store, lease=store.claim_ready("new"), stage="clean.llm_semantic")
    with restored.step("clean.llm_semantic", {}) as scope:
        scope.output(invoke(restored))
    assert len(sent) == 1 and store.status()["format_reanswers"] == 0
    assert restored.model_call_id("ignored-proposal") == original_call_id
    assert store.status()["known_usage"]["total_tokens"] == 50
    verify_child_seal(store, restored.seal())


@pytest.mark.parametrize("window", ["before_retry_cas", "after_retry_cas", "after_retry_prepare"])
def test_strict_tool_reanswer_resume_retains_original_arguments_and_only_sends_second(strict_execution, tmp_path, monkeypatch, window):
    from test_deepseek_strict_clean import (complete, native as tool_native, real_http,
                                           FINAL_SCHEMA, CLEAN_CONTRACT)
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    parent = strict_execution[0]
    clock = [BASE]
    store = WorkStore.create(tmp_path / "strict-resume", manifest(run_id=parent.run_id), clock=lambda: clock[0])
    store.plan([unit("tool")], originals=[])
    child = UnitExecutionContext(parent, work_store=store, lease=store.claim_ready("first"), stage="clean.llm_semantic")
    original_claim = child.claim_model_format_retry
    def crash():
        if window == "after_retry_cas":
            assert store.claim_format_retry(child.lease, child._task_attempt.attempt_id,
                eligibility_verifier=lambda request, native: True)
        elif window == "after_retry_prepare":
            assert original_claim()
        raise RuntimeError("TEST_FIXTURE strict reanswer preparation crash")
    monkeypatch.setattr(child, "claim_model_format_retry", crash)
    with real_http(monkeypatch, lambda *args: tool_native("{bad original arguments", identifier="TEST_FIXTURE_original_first")) as first_http:
        with pytest.raises(RuntimeError, match="preparation crash"):
            with child.step("clean.llm_semantic", {}):
                complete(child)
        assert len(first_http["wires"]) == 1
        original_wire = first_http["wires"][0]
    original_native = store.read_attempt_response(store.attempts_for_unit("tool")[0]["attempt_id"])
    assert original_native["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{bad original arguments"
    clock[0] += 61
    store.recover_expired()
    restored = UnitExecutionContext(parent, work_store=store, lease=store.claim_ready("new"), stage="clean.llm_semantic")
    client = DeepSeekRequiredClient(restored)
    with real_http(monkeypatch, lambda *args: tool_native(identifier="TEST_FIXTURE_original_second")) as second_http:
        with restored.step("clean.llm_semantic", {}) as scope:
            result = client.complete_json(system="TEST_FIXTURE strict data only", user="TEST_FIXTURE complete original sources",
                schema_hint={}, schema=FINAL_SCHEMA, clean_protocol={"contract": CLEAN_CONTRACT})
            scope.output(result)
        assert second_http["wires"] == [original_wire]
    assert client._remote_requests == 1
    assert store.status()["format_reanswers"] == 1 and store.status()["known_usage"]["total_tokens"] == 400
    assert store.read_attempt_response(store.attempts_for_unit("tool")[0]["attempt_id"]) == original_native
    verify_child_seal(store, restored.seal())
