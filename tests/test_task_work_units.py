"""Real SQLite task recovery, fencing, billing holds and isolated scheduling.

All external operations below are local deterministic callbacks. None creates
a provider client, uses a key, or grants the old paper profile a new contract.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier, Event, get_ident
import time

import pytest

from trading_system.agent_evidence import EvidenceStore, digest, json_bytes
from trading_system.redline.execution import EvidenceWriteError
from trading_system.tasks import (
    CONTRACT, Coordinator, UnitContext, UnitSpec, WorkStore, WorkUnitError,
    verify_document_segments,
)


BASE = datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp()
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def manifest(**changes):
    value = {
        "schema": CONTRACT, "tenant_id": "tenant-a", "run_id": "run-" + "a" * 32,
        "source_sha256": "1" * 64, "policy_sha256": "2" * 64,
        "input_sha256": "3" * 64, "as_of": "2026-11-08T00:00:00+00:00",
        "deadline_at": "2026-11-08T02:00:00+00:00", "format_retry_limit": 1,
    }
    return value | changes


def unit(name, *, dependencies=(), documents=(), terminal=(), inputs=None):
    return UnitSpec(
        unit_id=name, kind="extract", inputs={"fixture": name} if inputs is None else inputs,
        contract={"schema": "TEST_FIXTURE.extract/v1", "validator": "TEST_FIXTURE.exact/v1"},
        dependencies=tuple(dependencies), document_ids=tuple(documents),
        terminal_documents=tuple(terminal),
    )


def create(tmp_path, specs=None, originals=None, *, clock=None, **changes):
    store = WorkStore.create(tmp_path / "units", manifest(**changes), clock=clock or (lambda: BASE),
                             busy_timeout_ms=50)
    store.plan(specs or [unit("a")], originals=originals or [])
    return store


def valid(value):
    assert isinstance(value, dict) and value.get("ok") is True


def accept(store, lease, *, value=None, dispositions=None):
    return store.publish_result(lease, {"ok": True} if value is None else value,
                                validator=valid, dispositions=dispositions)


def observed(store, lease, *, ordinal=1, request=None, usage=USAGE):
    attempt = store.begin_attempt(lease, {"request": "fixture"} if request is None else request,
                                  operation="model.deepseek.chat", ordinal=ordinal,
                                  reservation={"output_tokens": 20})
    store.mark_dispatch(lease, attempt.attempt_id)
    receipt = store.record_response(lease, attempt.attempt_id,
                                    {"id": "fixture-native-" + str(ordinal), "ok": True}, usage=usage)
    if usage is not None:
        store.settle_attempt(attempt.attempt_id)
    return attempt, receipt


def test_new_contract_is_explicit_and_open_checks_every_manifest_field(tmp_path):
    store = create(tmp_path)
    again = WorkStore.open(store.root, manifest(), clock=lambda: BASE)
    assert again.status()["manifest_sha256"] == store.status()["manifest_sha256"]
    for changes in ({"schema": "tiger.execution-event.v1"}, {"tenant_id": "tenant-b"},
                    {"run_id": "run-" + "b" * 32}, {"policy_sha256": "4" * 64},
                    {"input_sha256": "5" * 64}, {"format_retry_limit": 2}):
        with pytest.raises(WorkUnitError):
            WorkStore.open(store.root, manifest(**changes), clock=lambda: BASE)


def test_old_evidence_stream_remains_nonresumable(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "events.jsonl").write_bytes(b"old immutable fixture\n")
    with pytest.raises(EvidenceWriteError):
        EvidenceStore(old, "old-run")
    create(tmp_path)
    assert (old / "events.jsonl").read_bytes() == b"old immutable fixture\n"


def test_dag_join_uses_verified_dependency_hashes_and_reuses_result(tmp_path):
    store = create(tmp_path, [unit("a"), unit("b", dependencies=("a",))])
    a = store.claim_ready("worker-a")
    assert a.unit_id == "a"
    assert store.claim_ready("worker-b") is None
    ref = accept(store, a)
    b = store.claim_ready("worker-b")
    assert b.unit_id == "b"
    accept(store, b)
    assert store.read_result("a")["value"] == {"ok": True}
    assert store.read_result("b")["dependency_results"] == {"a": ref["sha256"]}
    again = WorkStore.open(store.root, manifest(), clock=lambda: BASE)
    assert again.resume_plan()["reusable"] == ["a", "b"]
    assert again.claim_ready("worker-c") is None
    assert again.verify()["unit_count"] == 2


@pytest.mark.parametrize("specs", [
    [unit("a", dependencies=("absent",))],
    [unit("a", dependencies=("b",)), unit("b", dependencies=("a",))],
    [unit("a", dependencies=("a",))],
    [unit("a"), unit("a")],
])
def test_invalid_graph_is_rejected_atomically(tmp_path, specs):
    store = WorkStore.create(tmp_path / "units", manifest(), clock=lambda: BASE)
    with pytest.raises(WorkUnitError):
        store.plan(specs, originals=[])
    assert store.status()["units"] == []


def test_plan_is_idempotent_but_cannot_silently_replace_scope(tmp_path):
    store = create(tmp_path)
    before = store.status()["plan_sha256"]
    assert store.plan([unit("a")], originals=[]) == before
    with pytest.raises(WorkUnitError):
        store.plan([unit("a"), unit("b")], originals=[])
    assert store.status()["plan_sha256"] == before


def test_two_connections_claim_one_unit_and_old_epoch_is_fenced(tmp_path):
    clock = [BASE]
    store = create(tmp_path, clock=lambda: clock[0])
    barrier = Barrier(2)
    def claim(worker):
        barrier.wait(2)
        return store.claim_ready(worker, lease_seconds=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["worker-a", "worker-b"]))
    lease = next(x for x in outcomes if x is not None)
    assert sum(x is not None for x in outcomes) == 1
    clock[0] += 3
    assert store.recover_expired()["ready"] == ["a"]
    newer = store.claim_ready("worker-new")
    assert newer.epoch == lease.epoch + 1
    with pytest.raises(WorkUnitError, match="lease"):
        accept(store, lease)
    accept(store, newer)
    assert store.read_result("a")["epoch"] == newer.epoch


def test_lease_heartbeat_cannot_revive_expired_or_foreign_owner(tmp_path):
    clock = [BASE]
    store = create(tmp_path, clock=lambda: clock[0])
    lease = store.claim_ready("worker", lease_seconds=2)
    renewed = store.heartbeat(lease, lease_seconds=4)
    assert renewed.until == BASE + 4
    clock[0] += 5
    with pytest.raises(WorkUnitError):
        store.heartbeat(renewed)
    with pytest.raises(WorkUnitError):
        store.publish_result(lease, {"ok": True}, validator=valid)


def test_crash_before_dispatch_releases_only_known_not_sent_reservation(tmp_path):
    clock = [BASE]
    store = create(tmp_path, clock=lambda: clock[0])
    lease = store.claim_ready("worker", lease_seconds=1)
    a = store.begin_attempt(lease, {"request": 1}, operation="model.deepseek.chat",
                            reservation={"output_tokens": 20})
    clock[0] += 2
    store.recover_expired()
    assert store.attempt(a.attempt_id)["state"] == "not_sent"
    assert store.status()["unknown_attempts"] == 0
    assert store.status()["held_reservations"] == []
    assert store.claim_ready("worker-next") is not None


def test_dispatch_unknown_survives_expiry_reopen_and_blocks_new_external_send(tmp_path):
    clock = [BASE]
    store = create(tmp_path, [unit("a"), unit("b")], clock=lambda: clock[0])
    lease = store.claim("a", "worker", lease_seconds=1)
    a = store.begin_attempt(lease, {"request": 1}, operation="model.deepseek.chat",
                            reservation={"output_tokens": 20})
    store.mark_dispatch(lease, a.attempt_id)
    clock[0] += 2
    store.recover_expired()
    reopened = WorkStore.open(store.root, manifest(), clock=lambda: clock[0])
    assert reopened.status()["unknown_attempts"] == 1
    assert reopened.status()["held_reservations"][0]["attempt_id"] == a.attempt_id
    b = reopened.claim("b", "other")
    with pytest.raises(WorkUnitError, match="reconcil"):
        reopened.begin_attempt(b, {"request": 2}, operation="model.deepseek.chat")
    with pytest.raises(WorkUnitError):
        reopened.retry_units(["a"])


def test_late_native_is_preserved_but_requires_explicit_verified_reconciliation(tmp_path):
    clock = [BASE]
    store = create(tmp_path, clock=lambda: clock[0])
    lease = store.claim_ready("old", lease_seconds=1)
    a = store.begin_attempt(lease, {"request": 1}, operation="model.deepseek.chat")
    store.mark_dispatch(lease, a.attempt_id)
    clock[0] += 2
    store.recover_expired()
    receipt = store.record_response(lease, a.attempt_id, {"id": "actual-native"}, usage=USAGE)
    assert receipt["late"] is True
    assert store.status()["unknown_attempts"] == 1
    with pytest.raises(WorkUnitError):
        accept(store, lease)
    def reject(*args):
        raise ValueError("untrusted receipt")
    with pytest.raises(WorkUnitError):
        store.reconcile_attempt(a.attempt_id, receipt["observation_id"], verifier=reject)
    assert store.status()["unknown_attempts"] == 1
    store.reconcile_attempt(a.attempt_id, receipt["observation_id"],
                            verifier=lambda request, response, usage: None)
    assert store.status()["unknown_attempts"] == 0
    store.retry_units(["a"])
    newer = store.claim_ready("new")
    same = store.begin_attempt(newer, {"request": 1}, operation="model.deepseek.chat")
    assert same.reused and store.read_attempt_response(same.attempt_id) == {"id": "actual-native"}
    accept(store, newer)


def test_response_saved_before_unit_commit_reuses_without_second_external_effect(tmp_path):
    clock = [BASE]
    store = create(tmp_path, clock=lambda: clock[0])
    lease = store.claim_ready("old", lease_seconds=1)
    a, _ = observed(store, lease)
    clock[0] += 2
    store.recover_expired()
    lease2 = store.claim_ready("new")
    cached = store.begin_attempt(lease2, {"request": "fixture"}, operation="model.deepseek.chat",
                                 reservation={"output_tokens": 20})
    assert cached.attempt_id == a.attempt_id and cached.reused
    accept(store, lease2)
    assert store.status()["known_usage"] == USAGE
    assert store.status()["settled_attempts"] == 1


def test_http400_can_fail_business_with_usage_still_unknown_and_no_retry(tmp_path):
    store = create(tmp_path)
    lease = store.claim_ready("worker")
    a, _ = observed(store, lease, usage=None)
    store.fail(lease, "HTTP_ERROR_400", retryable=False)
    assert store.unit("a")["state"] == "failed"
    assert store.status()["unknown_attempts"] == 1
    assert store.status()["known_usage"] is None
    with pytest.raises(WorkUnitError):
        store.retry_units(["a"])
    with pytest.raises(WorkUnitError):
        store.claim_format_retry(lease, a.attempt_id, eligibility_verifier=lambda *a: True)


def test_format_retry_is_global_durable_cas_and_requires_known_replayed_eligibility(tmp_path):
    store = create(tmp_path, [unit("a"), unit("b")])
    leases = [store.claim(name, "worker-" + name) for name in ["a", "b"]]
    attempts = [observed(store, lease)[0] for lease in leases]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda pair: store.claim_format_retry(
            pair[0], pair[1].attempt_id, eligibility_verifier=lambda request, response: True),
            zip(leases, attempts)))
    assert sum(claims) == 1
    again = WorkStore.open(store.root, manifest(), clock=lambda: BASE)
    assert again.status()["format_reanswers"] == 1
    winner = claims.index(True)
    with pytest.raises(WorkUnitError):
        again.begin_attempt(leases[winner], {"different": "wire"}, operation="model.deepseek.chat", ordinal=2)
    second = again.begin_attempt(leases[winner], {"request": "fixture"},
                                  operation="model.deepseek.chat", ordinal=2,
                                  reservation={"output_tokens": 20})
    assert second.ordinal == 2
    assert not again.claim_format_retry(leases[1-winner], attempts[1-winner].attempt_id,
                                         eligibility_verifier=lambda *a: True)


def test_failed_eligibility_or_unknown_cannot_claim_retry_by_diagnostic_flag(tmp_path):
    store = create(tmp_path)
    lease = store.claim_ready("worker")
    a, _ = observed(store, lease)
    assert not store.claim_format_retry(lease, a.attempt_id,
                                        eligibility_verifier=lambda request, response: False)
    assert store.status()["format_reanswers"] == 0
    with pytest.raises(WorkUnitError):
        store.begin_attempt(lease, {"request": "fixture"}, operation="model.deepseek.chat", ordinal=2)


def test_237_originals_have_terminal_dispositions_without_forcing_eligibility(tmp_path):
    originals = [{"id": f"doc{i}", "sha256": digest(str(i).encode())} for i in range(237)]
    ids = tuple(row["id"] for row in originals)
    store = create(tmp_path, [unit("all", documents=ids, terminal=ids)], originals)
    lease = store.claim_ready("worker")
    dispositions = {name: {"state": "eligible" if i < 200 else "irrelevant" if i < 220 else "quarantined",
                           "reason": "TEST_FIXTURE.source-bound-validation"}
                    for i, name in enumerate(ids)}
    accept(store, lease, dispositions=dispositions)
    summary = store.document_dispositions()
    assert summary["admitted"] == summary["terminal"] == 237
    assert summary["eligible"] == 200 and summary["pending"] == []
    assert store.verify()["document_count"] == 237


def test_missing_extra_or_ambiguous_document_terminal_is_rejected(tmp_path):
    originals = [{"id": "doc1", "sha256": "1" * 64}]
    store = create(tmp_path, [unit("a", documents=("doc1",), terminal=("doc1",))], originals)
    lease = store.claim_ready("worker")
    for value in [{}, {"other": {"state": "eligible", "reason": "fixture"}},
                  {"doc1": {"state": "accepted", "reason": "fixture"}}]:
        with pytest.raises(WorkUnitError):
            accept(store, lease, dispositions=value)
    assert store.document_dispositions()["pending"] == ["doc1"]


def test_result_hash_corruption_is_never_cached_as_success(tmp_path):
    store = create(tmp_path)
    ref = accept(store, store.claim_ready("worker"))
    (store.root / ref["path"]).write_bytes(b"{}\n")
    for fn in [lambda: store.read_result("a"), store.resume_plan, store.verify]:
        with pytest.raises(WorkUnitError):
            fn()


def test_result_publish_crash_leaves_orphan_not_validated_and_can_resume(tmp_path, monkeypatch):
    clock = [BASE]
    store = create(tmp_path, clock=lambda: clock[0])
    lease = store.claim_ready("worker", lease_seconds=1)
    original = store._commit_result
    def crash(*args, **kwargs):
        raise RuntimeError("TEST_FIXTURE crash after blob publication")
    monkeypatch.setattr(store, "_commit_result", crash)
    with pytest.raises(RuntimeError):
        accept(store, lease)
    assert list((store.root / "artifacts").glob("*.json"))
    assert store.unit("a")["result_ref"] is None
    monkeypatch.setattr(store, "_commit_result", original)
    clock[0] += 2
    store.recover_expired()
    accept(store, store.claim_ready("next"))
    assert store.read_result("a")["value"] == {"ok": True}


def test_sql_busy_corrupt_json_and_symlinks_have_explicit_failure(tmp_path):
    store = create(tmp_path)
    connection = sqlite3.connect(store.database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(WorkUnitError) as caught:
            store.claim_ready("busy")
        assert caught.value.code == "STORE_BUSY"
    finally:
        connection.rollback()
        connection.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkUnitError):
        WorkStore.create(tmp_path / "linked" / "units", manifest())
    symlink_root = tmp_path / "symlink-db"
    symlink_root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"not a sqlite database")
    (symlink_root / "work-units.sqlite3").symlink_to(secret)
    with pytest.raises(WorkUnitError):
        WorkStore.create(symlink_root, manifest())
    assert secret.read_bytes() == b"not a sqlite database"


def test_cancel_stops_dispatch_but_preserves_unknown_reservation(tmp_path):
    store = create(tmp_path, [unit("a"), unit("b")])
    lease = store.claim("a", "worker")
    a = store.begin_attempt(lease, {"fixture": 1}, operation="model.deepseek.chat",
                            reservation={"output_tokens": 20})
    store.mark_dispatch(lease, a.attempt_id)
    status = store.cancel(reason="TEST_FIXTURE.owner_cancel")
    assert status["graph_state"] == "cancelling"
    assert status["unknown_attempts"] == 1 and status["held_reservations"]
    assert store.claim_ready("new") is None
    assert store.unit("b")["state"] == "cancelled"
    with pytest.raises(WorkUnitError):
        accept(store, lease)


def test_validator_must_run_and_cannot_return_a_success_flag(tmp_path):
    store = create(tmp_path)
    lease = store.claim_ready("worker")
    for validator in [None, lambda value: True, lambda value: {"validated": True}]:
        with pytest.raises(WorkUnitError):
            store.publish_result(lease, {"ok": True}, validator=validator)
    with pytest.raises(WorkUnitError):
        store.publish_result(lease, {"ok": False}, validator=valid)
    assert store.unit("a")["state"] == "running"


def test_context_owner_isolated_and_coordinator_cannot_be_waited_from_worker(tmp_path):
    store = create(tmp_path, [unit("a"), unit("b", dependencies=("a",))])
    clients, threads = [], []
    class Client:
        def __init__(self):
            self.owner = get_ident()
    coordinator = Coordinator(store, max_workers=1, client_factory=lambda context: Client())
    def handle(context):
        clients.append(context.client)
        threads.append(get_ident())
        assert context.client.owner == get_ident()
        with pytest.raises(WorkUnitError):
            coordinator.wait(timeout=0)
        errors = []
        def wrong_thread():
            try:
                context.check_active()
            except WorkUnitError as exc:
                errors.append(exc.code)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(wrong_thread).result(timeout=2)
        assert errors == ["CONTEXT_OWNER_MISMATCH"]
        return {"ok": True}
    try:
        result = coordinator.run({"extract": handle}, validators={"extract": valid}, timeout=3)
    finally:
        coordinator.close()
    assert result["graph_state"] == "completed"
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert all(thread != get_ident() for thread in threads)


def test_stop_observing_does_not_cancel_accepted_work(tmp_path):
    store = create(tmp_path)
    entered, release = Event(), Event()
    coordinator = Coordinator(store, max_workers=1)
    def handle(context):
        entered.set()
        assert release.wait(2)
        return {"ok": True}
    try:
        coordinator.tick({"extract": handle}, validators={"extract": valid})
        assert entered.wait(2)
        coordinator.stop_observing()
        assert not store.status()["cancel_requested"]
        release.set()
        coordinator.close()
        assert store.read_result("a")["value"] == {"ok": True}
        assert not store.status()["cancel_requested"]
    finally:
        release.set()
        coordinator.close()


def test_shared_mutable_client_factory_is_rejected(tmp_path):
    store = create(tmp_path, [unit("a"), unit("b")])
    shared = object()
    coordinator = Coordinator(store, max_workers=2, client_factory=lambda context: shared)
    def handle(context):
        context.client
        return {"ok": True}
    try:
        result = coordinator.run({"extract": handle}, validators={"extract": valid}, timeout=3)
    finally:
        coordinator.close()
    assert result["graph_state"] == "failed"
    assert any(row["error_code"] == "CLIENT_CONTEXT_SHARED" for row in result["units"])


def test_full_segment_manifest_preserves_unicode_offsets_and_overlap_context():
    text = "第一章\n正文负面事实。第二章\n条件与反证。"
    split = text.index("第二章")
    segments = [{"id": "s1", "start": 0, "end": split,
                 "sha256": digest(text[:split].encode()), "context_start": 0,
                 "context_end": len(text), "context_sha256": digest(text.encode())},
                {"id": "s2", "start": split, "end": len(text),
                 "sha256": digest(text[split:].encode())}]
    result = verify_document_segments("doc", text, segments)
    assert result["characters"] == len(text) and result["complete"] is True
    assert result["source_sha256"] == digest(text.encode())
    for change in [lambda s: s[1].update(start=split + 1),
                   lambda s: s[1].update(start=split - 1),
                   lambda s: s[1].update(sha256="0" * 64),
                   lambda s: s[1].update(id="s1"),
                   lambda s: s[0].update(context_sha256="0" * 64)]:
        copied = json.loads(json.dumps(segments))
        change(copied)
        with pytest.raises(WorkUnitError):
            verify_document_segments("doc", text, copied)


def test_real_process_crash_after_dispatch_cannot_be_retried(tmp_path):
    store = create(tmp_path)
    script = """
import json,os,sys
from trading_system.tasks import WorkStore
root,manifest=sys.argv[1],json.loads(sys.argv[2])
s=WorkStore.open(root,manifest,clock=lambda:1794096000.0)
l=s.claim_ready('child',lease_seconds=1)
a=s.begin_attempt(l,{'fixture':1},operation='model.deepseek.chat',reservation={'output_tokens':20})
s.mark_dispatch(l,a.attempt_id)
os._exit(73)
"""
    process = subprocess.run([sys.executable, "-B", "-c", script, str(store.root), json.dumps(manifest())],
                             cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=10)
    assert process.returncode == 73, process.stderr
    later = WorkStore.open(store.root, manifest(), clock=lambda: BASE + 2)
    later.recover_expired()
    assert later.status()["unknown_attempts"] == 1
    assert later.claim_ready("parent") is None
    assert later.status()["held_reservations"]
