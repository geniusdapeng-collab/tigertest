"""Task resource limits constrain actual concurrently executing local handlers."""
from threading import Event, Lock

import pytest

from test_task_work_units import accept, create, unit, valid
from trading_system.tasks import Coordinator, UnitResult, WorkUnitError


def test_weighted_tokens_and_memory_allow_only_work_that_fits_then_release(tmp_path):
    store = create(tmp_path, [unit(name, inputs={"tokens": tokens, "memory_bytes": memory})
                              for name, tokens, memory in [("a", 6, 40), ("b", 5, 10), ("c", 4, 60)]])
    store.busy_timeout_ms = 5000
    entered, release = Event(), Event()
    running, peak, lock = [], [{"tokens": 0, "memory_bytes": 0}], Lock()
    coordinator = Coordinator(store, max_workers=4,
        resource_capacity={"tokens": 10, "memory_bytes": 100}, resource_cost=lambda spec: spec.inputs)
    def handler(context):
        with lock:
            running.append(context.lease.unit_id)
            current = {name: sum(store.unit(unit_id)["spec"]["inputs"][name] for unit_id in running)
                       for name in ("tokens", "memory_bytes")}
            peak.append(current)
            if set(running) == {"a", "c"}:
                entered.set()
        assert release.wait(3)
        with lock:
            running.remove(context.lease.unit_id)
        return {"ok": True}
    try:
        coordinator.tick({"extract": handler}, validators={"extract": valid})
        assert entered.wait(2)
        assert store.unit("b")["state"] == "ready"
        assert coordinator.wait(timeout=0)["resources"]["used"] == {"tokens": 10, "memory_bytes": 100}
        release.set()
        result = coordinator.run({"extract": handler}, validators={"extract": valid}, timeout=3)
        assert result["graph_state"] == "completed", [(row["unit_id"], row["state"], row["error_code"]) for row in result["units"]]
        assert result["resources"]["used"] == {"tokens": 0, "memory_bytes": 0}
        assert all(row["tokens"] <= 10 and row["memory_bytes"] <= 100 for row in peak)
    finally:
        release.set()
        coordinator.close()


def test_over_capacity_head_is_failed_without_blocking_later_admissible_work(tmp_path):
    store = create(tmp_path, [unit("a-large", inputs={"tokens": 11}), unit("b-small", inputs={"tokens": 1})])
    sent = []
    coordinator = Coordinator(store, max_workers=4, resource_capacity={"tokens": 10},
                              resource_cost=lambda spec: spec.inputs)
    try:
        result = coordinator.run({"extract": lambda context: sent.append(context.lease.unit_id) or {"ok": True}},
                                 validators={"extract": valid}, timeout=3)
        assert sent == ["b-small"] and result["graph_state"] == "failed"
        assert store.unit("a-large")["error_code"] == "RESOURCE_EXCEEDS_CAPACITY"
        assert result["resources"]["used"] == {"tokens": 0}
    finally:
        coordinator.close()


def test_handler_exception_releases_full_weight_for_next_unit(tmp_path):
    store = create(tmp_path, [unit("a"), unit("b")])
    coordinator = Coordinator(store, max_workers=2, resource_capacity={"memory_bytes": 100},
                              resource_cost=lambda spec: {"memory_bytes": 100})
    seen = []
    def handler(context):
        seen.append(context.lease.unit_id)
        if context.lease.unit_id == "a":
            raise ValueError("TEST_FIXTURE never persisted provider text")
        return {"ok": True}
    try:
        result = coordinator.run({"extract": handler}, validators={"extract": valid}, timeout=3)
        assert seen == ["a", "b"]
        assert store.unit("a")["error_code"] == "UNIT_HANDLER_FAILED"
        assert store.read_result("b")["value"] == {"ok": True}
        assert result["resources"]["used"] == {"memory_bytes": 0}
    finally:
        coordinator.close()


def test_handler_validation_keeps_only_whitelisted_error_code(tmp_path):
    store = create(tmp_path, [unit("invalid"), unit("opaque")])
    coordinator = Coordinator(store, max_workers=1)
    def handler(context):
        if context.lease.unit_id == "invalid":
            raise ValueError("WORK_ANNOTATION_INVALID:TEST_FIXTURE model text must not persist")
        raise ValueError("TEST_FIXTURE arbitrary implementation detail")
    try:
        result = coordinator.run({"extract": handler}, validators={"extract": valid}, timeout=3)
        assert result["graph_state"] == "failed"
        assert store.unit("invalid")["error_code"] == "WORK_ANNOTATION_INVALID"
        assert store.unit("opaque")["error_code"] == "UNIT_HANDLER_FAILED"
    finally:
        coordinator.close()


def test_transient_store_busy_is_recorded_as_retryable_not_permanent(tmp_path):
    store = create(tmp_path, [unit("busy")])
    coordinator = Coordinator(store, max_workers=1)
    try:
        def handler(context):
            raise WorkUnitError("STORE_BUSY", "TEST_FIXTURE transient SQLite contention")
        result = coordinator.run({"extract": handler}, validators={"extract": valid}, timeout=3)
        row = store.unit("busy")
        assert result["graph_state"] == "failed"
        assert row["error_code"] == "STORE_BUSY" and row["retryable"] == 1
        store.retry_units(["busy"])
        assert store.unit("busy")["state"] == "ready"
    finally:
        coordinator.close()


def test_store_busy_while_planning_resource_cost_is_retryable(tmp_path):
    store = create(tmp_path, [unit("busy-plan")])
    def cost(_spec):
        raise WorkUnitError("STORE_BUSY", "TEST_FIXTURE resource read contention")
    coordinator = Coordinator(store, max_workers=1, resource_capacity={"tokens": 10},
                              resource_cost=cost)
    try:
        coordinator.run({"extract": lambda context: pytest.fail("must not dispatch")},
                        validators={"extract": valid}, timeout=3)
        row = store.unit("busy-plan")
        assert row["error_code"] == "STORE_BUSY" and row["retryable"] == 1
    finally:
        coordinator.close()


def test_saved_infrastructure_failure_can_be_reclassified_only_without_unknown_attempts(tmp_path):
    store = create(tmp_path, [unit("legacy-busy")])
    lease = store.claim_ready("stopped-worker")
    store.fail(lease, "STORE_BUSY", retryable=False)
    assert store.requeue_transient(
        ["legacy-busy"], allowed_codes={"STORE_BUSY"},
        reason="TEST_FIXTURE pre-fix scheduler contention") == ["legacy-busy"]
    assert store.unit("legacy-busy")["state"] == "ready"

    second = create(tmp_path / "second", [unit("unknown")])
    lease = second.claim_ready("stopped-worker")
    attempt = second.begin_attempt(lease, {"TEST_FIXTURE": True}, operation="model.deepseek.chat")
    second.mark_dispatch(lease, attempt.attempt_id)
    second.fail(lease, "ATTEMPT_UNKNOWN", retryable=False)
    with pytest.raises(WorkUnitError) as caught:
        second.requeue_transient(
            ["unknown"], allowed_codes={"ATTEMPT_UNKNOWN"},
            reason="TEST_FIXTURE must reconcile first")
    assert caught.value.code == "ATTEMPT_UNKNOWN"


def test_terminal_unit_result_passes_dispositions_once_through_coordinator(tmp_path):
    store = create(tmp_path, [unit("review", documents=("doc",), terminal=("doc",))],
                   [{"id": "doc", "sha256": "1" * 64}])
    coordinator = Coordinator(store)
    def handler(context):
        return UnitResult({"ok": True}, {"doc": {"state": "irrelevant", "reason": "TEST_FIXTURE independent review"}})
    try:
        assert coordinator.run({"extract": handler}, validators={"extract": valid}, timeout=3)["graph_state"] == "completed"
        assert store.document_dispositions()["irrelevant"] == 1
    finally:
        coordinator.close()


@pytest.mark.parametrize("value", [{"tokens": -1}, {"tokens": True}, {"unknown": 1}, None])
def test_bad_resource_estimate_fails_unit_without_any_handler_effect(tmp_path, value):
    store = create(tmp_path)
    coordinator = Coordinator(store, resource_capacity={"tokens": 10}, resource_cost=lambda spec: value)
    try:
        result = coordinator.run({"extract": lambda context: pytest.fail("must not dispatch")},
                                 validators={"extract": valid}, timeout=3)
        assert result["units"][0]["error_code"] == "RESOURCE_COST_INVALID"
        assert result["resources"]["used"] == {"tokens": 0}
    finally:
        coordinator.close()
