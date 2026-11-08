"""Real owner-fenced source evidence across concurrent local callbacks."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from test_deepseek_json_observation import observed
from test_research_work_execution import prepare
from trading_system.agent_evidence import read_snapshot
from trading_system.intelligence.contracts import SourceError
from trading_system.research_sources import SourceObservations, expand_source_join
from test_intelligence_acquisition import web
from test_repair_source_concurrency import setup_service


def request(number):
    return {"schema": "tiger.intelligence-source-request/v1", "job_id": "job-fixture",
        "attempt_id": "attempt-" + str(number), "source_id": "source-" + str(number),
        "source_version": "1", "operation": "fetch", "parameters": {"url": "https://example.com/"},
        "request_sha256": "1" * 64, "registry_sha256": "2" * 64}


def test_concurrent_source_children_join_on_parent_owner_and_keep_errors(observed):
    store, _, _ = prepare(observed)
    sources = SourceObservations(observed)
    barrier = Barrier(2)
    def call(number):
        def invoke():
            barrier.wait(timeout=3)
            if number == 2:
                raise SourceError("SOURCE_UNAVAILABLE", delivery="returned", http_status=503)
            return {"fixture": "original-response"}
        try:
            return sources.observer("source-" + str(number))("intelligence.source.request", request(number), invoke)
        except SourceError:
            return "failed"
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "source concurrency"}) as scope:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(call, [1, 2])) == [{"fixture": "original-response"}, "failed"]
        sources.drain()
        assert len(scope.io_calls) == 2
        expanded = []
        for joined in scope.io_calls:
            assert joined["method"] == "tasks.source.join"
            read = lambda ref, role: read_snapshot(observed.store.root, ref, run_id=observed.run_id,
                stage="clean.llm_semantic", role=role)
            inputs, output = read(joined["input_ref"], "input"), read(joined["output_ref"], "output")
            expanded.append(expand_source_join(observed.store.root, store, "clean.llm_semantic", inputs, output))
            changed = deepcopy(output); changed["request"]["source_id"] = "other-source"
            with pytest.raises(ValueError):
                expand_source_join(observed.store.root, store, "clean.llm_semantic", inputs, changed)
        assert sum("error_ref" in call for call in expanded) == 1
        scope.output({"observed": 2})
    assert len(list((store.root / "source-children").glob("*/events.jsonl"))) == 2


def test_duplicate_source_attempt_never_invokes_again(observed):
    prepare(observed)
    sources = SourceObservations(observed)
    calls = []
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "source reuse"}) as scope:
        observe = sources.observer("source-1")
        assert observe("intelligence.source.request", request(1), lambda: calls.append(1) or {"ok": True}) == {"ok": True}
        assert observe("intelligence.source.request", request(1), lambda: calls.append(2)) == {"ok": True}
        sources.drain()
        assert len(scope.io_calls) == 1 and calls == [1]
        scope.output({"observed": 1})


def test_source_factory_cannot_rebind_request_identity(observed):
    prepare(observed)
    sources = SourceObservations(observed)
    calls = []
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "identity"}) as scope:
        with pytest.raises(ValueError):
            sources.observer("source-2")("intelligence.source.request", request(1), lambda: calls.append(1))
        assert calls == []
        scope.output({"observed": 0})


def test_real_http_archive_attempts_are_joined_once_and_reused(observed, tmp_path, web):
    store, _, _ = prepare(observed)
    sources = SourceObservations(observed)
    service, job, routes, calls, _ = setup_service(tmp_path / "archive", web,
        observer_factory=sources.observer, observe_io=observed.call_io)
    barrier = Barrier(2)
    def response(_):
        barrier.wait(timeout=3)
        return 200, "application/json", b"{}"
    routes["/one"] = routes["/two"] = response
    with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "real loopback source"}) as scope:
        result = service.collect(job)
        sources.drain()
        assert len(scope.io_calls) == len(result["attempts"]) == len(calls) == 2
        assert result["source_runtime"]["peak_source_operations"] == 2
        service.collect(job)
        sources.drain()
        assert len(calls) == len(scope.io_calls) == 2
        assert store.status()["settled_attempts"] == 0  # Source archive is the sole attempt ledger.
        scope.output({"observed": 2})


def test_shared_parser_pool_waits_with_original_deadline():
    from threading import Event
    from trading_system.intelligence.parse_limits import ParserResourcePool
    pool = ParserResourcePool(max_workers=1, memory_bytes=10, temporary_bytes=10, wait_for_capacity=True)
    admitted, release = Event(), Event()
    def first():
        with pool.reserve(memory_bytes=10, temporary_bytes=10, timeout=2):
            admitted.set()
            assert release.wait(2)
    def second():
        with pool.reserve(memory_bytes=10, temporary_bytes=10, timeout=2):
            return pool.snapshot()["active_workers"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(first)
        assert admitted.wait(2)
        queued = executor.submit(second)
        assert not queued.done()
        release.set()
        active.result(timeout=2)
        assert queued.result(timeout=2) == 1
    with pool.reserve(memory_bytes=10, temporary_bytes=10, timeout=2):
        with pytest.raises(Exception) as denied:
            with pool.reserve(memory_bytes=1, temporary_bytes=1, timeout=0):
                raise AssertionError("overbooked")
        assert denied.value.code == "PARSER_DEADLINE_EXCEEDED"
    assert pool.snapshot()["active_workers"] == 0
