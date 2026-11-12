"""Append-only discovery waves retain one model retry meter and old results."""
import pytest

from test_task_work_units import BASE, accept, create, manifest, observed, unit
from trading_system.tasks import WorkStore, WorkUnitError


def test_new_wave_and_later_analysis_append_without_replacing_old_scope(tmp_path):
    store = create(tmp_path, [unit("doc1.extract", documents=("doc1",)),
                              unit("doc1.review", dependencies=("doc1.extract",), documents=("doc1",), terminal=("doc1",))],
                   [{"id": "doc1", "sha256": "1" * 64}])
    first_hash = store.status()["plan_sha256"]
    first = store.claim("doc1.extract", "worker")
    first_ref = accept(store, first)
    a, _ = observed(store, store.claim("doc1.review", "reviewer"))
    second_hash = store.extend_plan(
        [unit("doc2.extract", documents=("doc2",)),
         unit("doc2.review", dependencies=("doc2.extract",), documents=("doc2",), terminal=("doc2",))],
        originals=[{"id": "doc2", "sha256": "2" * 64}])
    assert first_hash != second_hash and store.status()["plan_revision"] == 2
    third_hash = store.extend_plan([unit("analysis", dependencies=("doc1.review", "doc2.review"),
                                        documents=("doc1", "doc2"))], originals=[])
    assert third_hash != second_hash and store.status()["plan_revision"] == 3
    assert store.read_result("doc1.extract")["value"] == {"ok": True}
    assert store.unit("doc1.extract")["result_ref"] == first_ref
    assert store.attempt(a.attempt_id)["state"] == "settled"
    assert store.verify()["unit_count"] == 5
    reopened = WorkStore.open(store.root, manifest(), clock=lambda: BASE)
    assert reopened.status()["plan_revision"] == 3
    assert reopened.resume_plan()["reusable"] == ["doc1.extract"]


def test_append_replay_is_idempotent_but_replacing_existing_input_hash_or_terminal_is_not(tmp_path):
    store = create(tmp_path, [unit("a", documents=("d1",), terminal=("d1",))],
                   [{"id": "d1", "sha256": "1" * 64}])
    new = unit("b", dependencies=("a",), documents=("d1",))
    ref = store.extend_plan([new], originals=[{"id": "d1", "sha256": "1" * 64}])
    assert store.extend_plan([new], originals=[{"id": "d1", "sha256": "1" * 64}]) == ref
    assert store.status()["plan_revision"] == 2
    invalid = [
        ([unit("a", inputs={"changed": True}, documents=("d1",), terminal=("d1",))], []),
        ([unit("b", dependencies=("a",), documents=("d1",))], [{"id": "d1", "sha256": "9" * 64}]),
        ([unit("twice", documents=("d1",), terminal=("d1",))], []),
        ([unit("missing", dependencies=("absent",))], []),
        ([], [{"id": "d2", "sha256": "2" * 64}]),
    ]
    for specs, originals in invalid:
        with pytest.raises(WorkUnitError):
            store.extend_plan(specs, originals=originals)
        assert store.status()["plan_sha256"] == ref and store.status()["plan_revision"] == 2
    with pytest.raises(WorkUnitError):
        store.plan([unit("a", documents=("d1",), terminal=("d1",))], originals=[{"id": "d1", "sha256": "1" * 64}])


def test_model_format_retry_remains_one_across_extensions_and_restart(tmp_path):
    store = create(tmp_path)
    lease = store.claim_ready("one")
    attempt, _ = observed(store, lease)
    assert store.claim_format_retry(lease, attempt.attempt_id, eligibility_verifier=lambda *args: True)
    accept(store, lease)
    store.extend_plan([unit("b")], originals=[])
    reopened = WorkStore.open(store.root, manifest(), clock=lambda: BASE)
    second_lease = reopened.claim("b", "two")
    second_attempt, _ = observed(reopened, second_lease)
    assert not reopened.claim_format_retry(second_lease, second_attempt.attempt_id,
                                           eligibility_verifier=lambda *args: True)
    assert reopened.status()["format_reanswers"] == 1
