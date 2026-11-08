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
