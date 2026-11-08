"""Concurrent source admission uses actual HTTP, durable quotas and atomic blobs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
import threading
import time

import pytest

from test_intelligence_acquisition import NOW, plan, web
from trading_system.intelligence import IntelligenceError, IntelligenceService, SourceRegistry, SourceSpec, verify_bundle
from trading_system.intelligence.archive import Archive
from trading_system.intelligence.contracts import SearchResult
from trading_system.intelligence.host_limits import HostRequestPolicy, bind_host_policy
from trading_system.intelligence.transport import HttpTransport


class HttpSource:
    def __init__(self, endpoint, transport):
        self.endpoint, self.transport = endpoint, transport

    def search(self, request):
        response = self.transport.request("GET", self.endpoint)
        return SearchResult([], request.to_dict(), {"response_url": response.url,
            "transport_sha256": response.transport_sha256, "received_at": response.received_at,
            "http_headers": response.headers, "request_count": response.request_count},
            {"state": "public_unmetered", "requires_reconciliation": False}, [], response.body)


def setup_service(tmp_path, web, *, workers=2, same_host=False, paid=False,
                  observer_factory=None, observe_io=None, limits=None):
    url, routes, calls = web
    second = url if same_host else url.replace("127.0.0.1", "localhost")
    specs = [SourceSpec("first", "feed", url + "/one", minimum_interval_seconds=0, max_retries=0),
             SourceSpec("second", "feed", second + "/two", minimum_interval_seconds=0, max_retries=0)]
    if paid:
        specs[1] = SourceSpec("second", "feed", second + "/two", minimum_interval_seconds=0,
                             max_retries=0, credential_env="TEST_SOURCE_KEY")
    reg = SourceRegistry(specs)
    shared = HttpTransport(allow_local=True, max_bytes=1024)
    adapters = {spec.source_id: HttpSource(spec.endpoint, shared) for spec in specs}
    svc = IntelligenceService(tmp_path, reg, adapters, clock=lambda: NOW, source_workers=workers,
        observer_factory=observer_factory, observe_io=observe_io)
    bind_host_policy(svc.adapters, HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    job = svc.create_job(plan(reg, **({"limits": limits} if limits is not None else {})), "concurrent-source")
    return svc, job["job_id"], routes, calls, shared


@pytest.mark.parametrize("workers,same_host,paid,expected", [(2, False, False, 2),
    (1, False, False, 1), (2, True, False, 1), (2, False, True, 1)])
def test_real_http_concurrency_and_host_paid_barriers(tmp_path, web, workers, same_host, paid, expected):
    svc, job, routes, calls, shared = setup_service(tmp_path, web,
        workers=workers, same_host=same_host, paid=paid)
    state, lock = {"active": 0, "peak": 0}, threading.Lock()
    overlap = threading.Barrier(2) if expected == 2 else None
    def response(_):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        if overlap:
            overlap.wait(timeout=3)
        else:
            time.sleep(.03)
        with lock:
            state["active"] -= 1
        return 200, "application/json", b"{}"
    routes["/one"] = routes["/two"] = response
    result = svc.collect(job)
    assert state["peak"] == expected and len(calls) == 2
    assert result["usage"]["requests"] == 2 and result["usage"]["bytes"] == 4
    assert not result["inflight_attempts"]
    assert all(row["status"] == "empty" for row in result["attempts"])
    assert svc.archive.verify_chain()["sequence"] > 0
    if workers > 1:
        assert result["source_runtime"]["configured_workers"] == workers
        assert result["source_runtime"]["peak_source_operations"] >= expected
        assert svc.adapters["first"].transport is not svc.adapters["second"].transport
        assert shared.total_request_count == 0
    else:
        assert result["effective_concurrency"] == 1 and "source_runtime" not in result
        assert svc.adapters["first"].transport is shared
    before = len(calls)
    svc.collect(job)
    assert len(calls) == before


def test_atomic_bytes_reservation_is_released_only_after_known_settlement(tmp_path):
    reg = SourceRegistry([SourceSpec("first", "feed", "https://example.org/", minimum_interval_seconds=0)])
    archive = Archive(tmp_path, clock=lambda: NOW)
    p = plan(reg, limits={"requests": 20, "fetches": 10, "bytes": 100,
        "rounds": 3, "concurrency": 4, "deadline_seconds": 100})
    job = archive.create_job(p, "reserved")["job_id"]
    request = {"parameters": {}, "reserved_requests": 6}
    first = archive.begin_attempt(job, "first", "search", request, reserve_bytes=100)
    with pytest.raises(IntelligenceError) as denied:
        archive.begin_attempt(job, "second", "search", request, reserve_bytes=100)
    assert denied.value.code == "RESOURCE_BUSY"
    archive.settle_attempt(job, first, "empty", {"request_count": 1, "bytes_received": 10,
        "usage": {"requires_reconciliation": False}})
    second = archive.begin_attempt(job, "second", "search", request, reserve_bytes=100)
    stored = next(row for row in archive.attempts(job) if row["attempt_id"] == second)
    assert stored["request"]["reserved_bytes"] == 90
    archive.settle_attempt(job, second, "unknown", {"request_count": 1, "bytes_received": 3,
        "usage": {"requires_reconciliation": True}})
    usage = archive.status(job)["usage"]
    assert usage["reserved_bytes"] == 90 and usage["reserved_requests"] == 6
    assert usage["requests"] == 2 and usage["bytes"] == 13
    with pytest.raises(IntelligenceError) as denied:
        archive.begin_attempt(job, "third", "search", request, reserve_bytes=100)
    assert denied.value.code == "RECONCILIATION_REQUIRED"


def test_atomic_reservations_do_not_oversell_in_threads(tmp_path):
    reg = SourceRegistry([SourceSpec("first", "feed", "https://example.org/", minimum_interval_seconds=0)])
    archive = Archive(tmp_path, clock=lambda: NOW)
    p = plan(reg, limits={"requests": 8, "fetches": 8, "bytes": 100,
        "rounds": 3, "concurrency": 4, "deadline_seconds": 100})
    job = archive.create_job(p, "racing-reservations")["job_id"]
    barrier = threading.Barrier(8)
    def reserve(index):
        barrier.wait(timeout=3)
        try:
            return archive.begin_attempt(job, "first", "fetch",
                {"parameters": {"index": index}, "reserved_requests": 2}, reserve_bytes=30)
        except IntelligenceError as exc:
            assert exc.code in {"RESOURCE_BUSY", "BUDGET_EXHAUSTED"}
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(reserve, range(8)))
    rows = archive.attempts(job)
    assert len([row for row in attempts if row]) == len(rows) == 4
    assert sum(row["request"]["reserved_bytes"] for row in rows) == 100
    assert sum(row["request"]["reserved_requests"] for row in rows) == 8


def test_blob_target_never_exposes_partial_writer(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    data = b"same concurrently received source original" * 100
    sha = hashlib.sha256(data).hexdigest()
    target = tmp_path / "blobs" / "sha256" / sha[:2] / sha
    first_opened, release = threading.Event(), threading.Event()
    real_fdopen, lock, count = os.fdopen, threading.Lock(), [0]
    @contextmanager
    def paused_fdopen(*args, **kwargs):
        with lock:
            count[0] += 1
            pause = count[0] == 1
        with real_fdopen(*args, **kwargs) as stream:
            if pause:
                first_opened.set()
                assert release.wait(3)
            yield stream
    monkeypatch.setattr(os, "fdopen", paused_fdopen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(archive.put_blob, data)
        assert first_opened.wait(3)
        try:
            assert not target.exists(), "immutable blob path is visible before fsync"
            second = pool.submit(archive.put_blob, data)
            second_ref = second.result(timeout=3)
        finally:
            release.set()
        first_ref = first.result(timeout=3)
    assert first_ref == second_ref and archive.read_blob(first_ref) == data
    assert not list(target.parent.glob(".*.pending"))


@pytest.mark.parametrize("workers", [True, 0, -1, 5, "2"])
def test_worker_configuration_is_bounded(tmp_path, workers):
    with pytest.raises(IntelligenceError) as denied:
        IntelligenceService(tmp_path, source_workers=workers)
    assert denied.value.code == "CONTRACT_INVALID"


def test_owner_thread_observer_fails_closed_without_factory(tmp_path):
    with pytest.raises(IntelligenceError) as denied:
        IntelligenceService(tmp_path, source_workers=2, observe_io=lambda *args: None)
    assert denied.value.code == "POLICY_DENIED"
    assert denied.value.details["reason"] == "SOURCE_OBSERVER_FACTORY_REQUIRED"
    assert not (tmp_path / "intelligence.sqlite3").exists()


def test_each_worker_owns_evidence_observer_before_actual_dispatch(tmp_path, web):
    lock, begun, owners = threading.Lock(), set(), {}
    def factory(source_id):
        owner = threading.get_ident()
        with lock:
            owners[source_id] = owner
        def observer(method, inputs, invoke):
            assert threading.get_ident() == owner
            assert method == "intelligence.source.request" and inputs["source_id"] == source_id
            with lock:
                begun.add(source_id)
            return invoke()
        return observer
    def parent_observer(*_):
        raise AssertionError("parent execution journal must stay on its owner thread")
    svc, job, routes, calls, _ = setup_service(tmp_path, web,
        observer_factory=factory, observe_io=parent_observer)
    barrier = threading.Barrier(2)
    def response(call):
        source = "first" if call["path"] == "/one" else "second"
        with lock:
            assert source in begun
        barrier.wait(timeout=3)
        return 200, "application/json", b"{}"
    routes["/one"] = routes["/two"] = response
    result = svc.collect(job)
    assert len(set(owners.values())) == 2 and len(calls) == 2
    assert result["source_runtime"]["peak_source_operations"] == 2


def test_reservation_contention_waits_then_reuses_unused_capacity(tmp_path, web):
    svc, job, routes, calls, _ = setup_service(tmp_path, web, limits={
        "requests": 2, "fetches": 2, "bytes": 4, "rounds": 1,
        "concurrency": 2, "deadline_seconds": 10})
    def response(_):
        time.sleep(.04)
        return 200, "application/json", b"{}"
    routes["/one"] = routes["/two"] = response
    result = svc.collect(job)
    assert len(calls) == 2 and len(result["attempts"]) == 2
    assert result["usage"]["requests"] == 2 and result["usage"]["bytes"] == 4
    assert result["usage"]["reserved_bytes"] == 0
    assert result["source_runtime"]["peak_source_operations"] == 1


def test_observation_crash_drains_peers_and_retry_never_repeats_unknown_dispatch(tmp_path, web):
    def factory(source_id):
        def observe(_method, _inputs, invoke):
            result = invoke()
            if source_id == "first":
                raise RuntimeError("fixture observer lost acknowledgement")
            return result
        return observe
    svc, job, routes, calls, _ = setup_service(tmp_path, web, observer_factory=factory)
    routes["/one"] = routes["/two"] = (200, "application/json", b"{}")
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        svc.collect(job)
    status = svc.status(job)
    assert len(calls) == 2 and not status["inflight_attempts"]
    assert sorted(row["status"] for row in status["attempts"]) == ["empty", "unknown"]
    assert status["source_runtime"]["active_source_operations"] == 0
    assert status["usage"]["reserved_bytes"] > 0
    with pytest.raises(IntelligenceError) as denied:
        svc.collect(job)
    assert denied.value.code == "RECONCILIATION_REQUIRED" and len(calls) == 2


def test_receipt_cannot_release_more_quota_than_reserved(tmp_path):
    reg = SourceRegistry([SourceSpec("first", "feed", "https://example.org/", minimum_interval_seconds=0)])
    archive = Archive(tmp_path, clock=lambda: NOW)
    job = archive.create_job(plan(reg), "invalid-settlement")["job_id"]
    attempt = archive.begin_attempt(job, "first", "fetch",
        {"parameters": {}, "reserved_requests": 1}, reserve_bytes=10)
    with pytest.raises(IntelligenceError) as denied:
        archive.settle_attempt(job, attempt, "completed", {"bytes_received": 11, "request_count": 1})
    assert denied.value.code == "CONTRACT_INVALID"
    status = archive.status(job)
    assert status["inflight_attempts"] == [attempt] and status["usage"]["reserved_bytes"] == 10


def test_parallel_registered_originals_and_common_attachment_freeze_and_replay(tmp_path, web):
    url, routes, calls = web
    endpoints = [url + "/first", url.replace("127.0.0.1", "localhost") + "/second"]
    reg = SourceRegistry([SourceSpec(name, "page", endpoint,
        allowed_hosts=("127.0.0.1", "localhost"), minimum_interval_seconds=0, max_retries=0)
        for name, endpoint in zip(("first", "second"), endpoints)])
    transport = HttpTransport(allow_local=True, max_bytes=2048)
    svc = IntelligenceService(tmp_path, reg, reg.build_adapters(transport=transport),
        source_workers=2, clock=lambda: NOW)
    bind_host_policy(svc.adapters, HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    barrier = threading.Barrier(2)
    original = ("<html><head><title>Issuer evidence</title></head><body><article>"
        "<h1>Issuer evidence</h1><p>Original revenue observations and the source limitations.</p>"
        f'<a href="{url}/common.csv">Data table</a></article></body></html>')
    def page(_):
        barrier.wait(timeout=3)
        return 200, "text/html", original
    routes["/first"] = routes["/second"] = page
    routes["/common.csv"] = 200, "text/csv", "metric,value\nRevenue,42\n"
    p = plan(reg)
    job = svc.create_job(p, "originals-and-attachments")["job_id"]
    acquired = svc.collect(job)
    assert len(calls) == 4 and len(acquired["documents"]) == 4
    assert len({doc["raw_blob"]["sha256"] for doc in acquired["documents"]}) == 2
    assert acquired["usage"]["requests"] == 4 and acquired["usage"]["fetches"] == 2
    bundle = svc.freeze(job, expected_plan_hash=p["plan_hash"], expected_revision=acquired["revision"],
        expected_frontier_hash=p["frontier_manifest_hash"])
    checked = verify_bundle(tmp_path, bundle["bundle_id"], expected_plan_hash=p["plan_hash"],
        expected_registry_sha256=reg.public_manifest()["sha256"])
    assert checked["manifest_sha256"] == bundle["manifest_sha256"] and len(calls) == 4


def test_custom_host_restrictions_are_not_replaced_by_parallel_mode(tmp_path):
    transport = HttpTransport(allow_local=True, request_policy=object())
    adapter = HttpSource("https://example.org/", transport)
    reg = SourceRegistry([SourceSpec("first", "feed", adapter.endpoint)])
    with pytest.raises(IntelligenceError) as denied:
        IntelligenceService(tmp_path, reg, {"first": adapter}, source_workers=2)
    assert denied.value.code == "POLICY_DENIED"
    assert denied.value.details["reason"] == "SOURCE_HOST_POLICY_CONFLICT"
    assert transport.request_policy is not None


def test_invalid_evidence_factory_never_creates_attempt_or_dispatches(tmp_path, web):
    svc, job, _routes, calls, _ = setup_service(tmp_path, web, observer_factory=lambda _: None)
    with pytest.raises(IntelligenceError) as denied:
        svc.collect(job)
    assert denied.value.details["reason"] == "SOURCE_OBSERVER_INVALID"
    assert svc.archive.attempts(job) == [] and calls == []


def test_sec_identity_dependency_finishes_before_submissions_dispatch(tmp_path, web):
    url, routes, calls = web
    reg = SourceRegistry([SourceSpec("directory", "sec_directory", url + "/directory", minimum_interval_seconds=0),
        SourceSpec("submissions", "sec_submissions", url.replace("127.0.0.1", "localhost") + "/submissions",
            minimum_interval_seconds=0)])
    adapters = {spec.source_id: HttpSource(spec.endpoint, HttpTransport(allow_local=True, max_bytes=1024))
        for spec in reg.sources.values()}
    svc = IntelligenceService(tmp_path, reg, adapters, clock=lambda: NOW, source_workers=2)
    bind_host_policy(svc.adapters, HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    completed = threading.Event()
    def directory(_):
        time.sleep(.03)
        completed.set()
        return 200, "application/json", b"{}"
    def submissions(_):
        assert completed.is_set()
        return 200, "application/json", b"{}"
    routes["/directory"], routes["/submissions"] = directory, submissions
    job = svc.create_job(plan(reg), "sec-order")["job_id"]
    acquired = svc.collect(job)
    assert [row["path"] for row in calls] == ["/directory", "/submissions"]
    assert acquired["source_runtime"]["peak_source_operations"] == 1


def test_expired_deadline_can_resume_known_checkpoint_without_new_dispatch(tmp_path, web):
    svc, job, routes, calls, _ = setup_service(tmp_path, web)
    routes["/one"] = 200, "application/json", b"{}"
    parameters = {"query": "checkpoint source observation"}
    first = svc._call(job, "first", "search", parameters)
    svc._deadlines[job] = time.monotonic() - 1
    assert svc._call(job, "first", "search", parameters) == first
    assert len(calls) == 1 and len(svc.archive.attempts(job)) == 1
    with pytest.raises(IntelligenceError) as denied:
        svc._call(job, "second", "search", parameters)
    assert denied.value.code == "BUDGET_EXHAUSTED" and len(calls) == 1


def test_maximum_worker_configuration_still_obeys_plan_cap(tmp_path, web):
    svc, job, routes, calls, _ = setup_service(tmp_path, web, workers=4, limits={
        "requests": 30, "fetches": 10, "bytes": 1000000, "rounds": 1,
        "concurrency": 1, "deadline_seconds": 10})
    routes["/one"] = routes["/two"] = 200, "application/json", b"{}"
    result = svc.collect(job)
    assert len(calls) == 2
    assert result["source_runtime"]["configured_workers"] == 4
    assert result["source_runtime"]["admitted_workers"] == result["effective_concurrency"] == 1


def test_blob_flush_failure_has_no_visible_target_or_orphan_staging(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    def failed_flush(_descriptor):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(os, "fsync", failed_flush)
    with pytest.raises(IntelligenceError) as failed:
        archive.put_blob(b"not yet durable")
    assert failed.value.code == "ARCHIVE_UNAVAILABLE"
    assert [path for path in (tmp_path / "blobs").rglob("*") if path.is_file()] == []
