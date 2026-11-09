"""Durable scheduler and real acquisition integration with isolated fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from test_intelligence_acquisition import web

from trading_system.intelligence.catalog import SourceCatalog
from trading_system.intelligence.contracts import IntelligenceError, SearchResult, source_time
from trading_system.intelligence.monitoring import MonitoringStore, MonitoringWorker
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.transport import HttpTransport


class Clock:
    def __init__(self): self.value = datetime(2026, 11, 6, tzinfo=timezone.utc)
    def __call__(self): return self.value
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


def setup(tmp_path, *, sources=1, max_queue=128, daily_requests=20000, same_domain=False):
    clock = Clock()
    reg = SourceRegistry([SourceSpec("source" + str(i), "feed", "https://" + ("shared" if same_domain else str(i)) + ".example.com/feed", minimum_interval_seconds=0) for i in range(sources)])
    catalog = SourceCatalog(reg, {"sources": {key: {"material_types": ["news_events"], "cadence_seconds": 300} for key in reg.source_ids()}}, reviewed_entities={})
    store = MonitoringStore(tmp_path, clock=clock, catalog=catalog, max_queue=max_queue, daily_requests=daily_requests)
    topic = store.create_topic("Track original releases and counterevidence", idempotency_key="topic1")
    watch = store.create_watch(topic["topic_id"], reg.source_ids(), "core", "watch1")
    return store, topic, watch, clock


def outcome(version="1", *, requests=2, **extra):
    return {"acquisition_complete": True, "document_versions": [{"id": "doc1", "version": version}],
        "observations": [{"delivery": "returned", "http_status": 200}], "usage": {"requests": requests},
        "cursor": {"etag": version}, **extra}


def settle(store, owner="worker", **kwargs):
    store.enqueue_due(limit=min(32, store.max_queue))
    job = store.claim(owner, **kwargs)
    assert job
    store.complete(job["id"], owner, job["generation"], outcome())
    return job


def test_topic_and_watch_idempotency_and_tenant_boundary(tmp_path):
    store, topic, watch, _ = setup(tmp_path)
    assert store.create_topic(topic["question"], idempotency_key="topic1") == topic
    with pytest.raises(IntelligenceError) as error:
        store.create_topic("Different question", idempotency_key="topic1")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    other = MonitoringStore(tmp_path, tenant="other", catalog=store.catalog)
    for action in (lambda: other.topic(topic["topic_id"]), lambda: other.status(watch["watch_id"])):
        with pytest.raises(IntelligenceError): action()


def test_queue_is_bounded_and_duplicate_due_ticks_are_coalesced(tmp_path):
    store, _, _, _ = setup(tmp_path, sources=5, max_queue=2)
    first = store.enqueue_due(limit=2)
    assert len(first["enqueued"]) == 2 and first["reason"] == "QUEUE_FULL"
    assert store.enqueue_due(limit=2)["enqueued"] == []
    assert store.status()["queue"] == {"queued": 2}


def test_two_workers_cannot_claim_same_task_or_shared_domain(tmp_path):
    store, _, _, _ = setup(tmp_path, sources=2, same_domain=True)
    store.enqueue_due()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(store.claim, ["one", "two"]))
    assert sum(row is not None for row in claims) == 1


def test_expired_worker_cannot_advance_new_generation_cursor(tmp_path):
    store, _, _, clock = setup(tmp_path)
    store.enqueue_due()
    old = store.claim("old", lease_seconds=1)
    store.bind_acquisition(old["id"], "old", old["generation"], "intel_known")
    clock.advance(2)
    new = store.claim("new")
    assert new["id"] == old["id"] and new["generation"] == old["generation"] + 1
    assert new["acquisition_id"] == "intel_known"
    with pytest.raises(IntelligenceError) as error:
        store.complete(old["id"], "old", old["generation"], outcome("old"))
    assert error.value.code == "LEASE_CONFLICT"
    store.complete(new["id"], "new", new["generation"], outcome("new"))
    assert store.status()["sources"][0]["cursor"] == {"etag": "new"}


def test_repeated_same_content_is_quiet_but_check_success_advances(tmp_path):
    store, _, _, clock = setup(tmp_path)
    settle(store)
    initial = store.status()
    assert initial["pending_changes"][0]["revision"] == 1
    clock.advance(301)
    settle(store)
    after = store.status()
    assert after["pending_changes"][0]["revision"] == 1
    assert after["sources"][0]["last_success"] > initial["sources"][0]["last_success"]
    clock.advance(301)
    store.enqueue_due(); job = store.claim("w")
    store.complete(job["id"], "w", job["generation"], outcome("2"))
    assert store.status()["pending_changes"][0]["revision"] == 2


def test_304_does_not_create_new_material_change(tmp_path):
    store, _, _, clock = setup(tmp_path)
    settle(store)
    clock.advance(301)
    store.enqueue_due(); job = store.claim("w")
    store.complete(job["id"], "w", job["generation"], {"acquisition_complete": True, "document_versions": [],
        "not_modified": True, "observations": [{"http_status": 304}], "cursor": {"etag": "1"}, "usage": {"requests": 1}})
    assert store.status()["pending_changes"][0]["revision"] == 1


def test_retry_after_uses_full_interval_and_failure_does_not_refresh_success(tmp_path):
    store, _, _, clock = setup(tmp_path)
    settle(store)
    last = store.status()["sources"][0]["last_success"]
    clock.advance(301)
    store.enqueue_due(); job = store.claim("w")
    store.fail(job["id"], "w", job["generation"], "RATE_LIMITED", retry_after=7200)
    source = store.status()["sources"][0]
    assert source["next_due"] >= clock().timestamp() + 7200 and source["last_success"] == last
    clock.advance(3600)
    assert store.enqueue_due()["enqueued"] == []


def test_daily_quota_is_reserved_before_dispatch_and_actual_usage_settled(tmp_path):
    store, _, _, _ = setup(tmp_path, sources=2, daily_requests=24)
    store.enqueue_due()
    job = store.claim("one")
    assert job and store.claim("two") is None
    store.complete(job["id"], "one", job["generation"], outcome(requests=2))
    assert store.claim("two") is None
    budget = store.status()["budget"]
    assert sum(row["spent"] for row in budget) == 2 and sum(row["held"] for row in budget) == 0


def test_uncertain_delivery_keeps_reservation_and_does_not_advance_cursor(tmp_path):
    store, _, _, _ = setup(tmp_path)
    store.enqueue_due(); job = store.claim("w")
    store.fail(job["id"], "w", job["generation"], "SOURCE_TIMEOUT", requires_reconciliation=True)
    state = store.status()
    assert state["queue"]["reconciliation"] == 1 and state["budget"][0]["held"] == 24
    assert state["sources"][0]["last_success"] is None and state["sources"][0]["cursor"] == {}
    assert store.enqueue_due()["enqueued"] == []


def test_reopen_recovers_original_identity_cursor_and_pending_changes(tmp_path):
    store, topic, watch, clock = setup(tmp_path)
    settle(store)
    reopened = MonitoringStore(tmp_path, clock=clock, catalog=store.catalog)
    assert reopened.topic(topic["topic_id"]) == topic
    assert reopened.status(watch["watch_id"]) == store.status(watch["watch_id"])


def test_readonly_does_not_initialize_or_mutate(tmp_path):
    target = tmp_path / "missing"
    store = MonitoringStore(target, readonly=True)
    assert store.status()["queue"] == {} and not target.exists()
    with pytest.raises(IntelligenceError) as error:
        store.create_topic("Question", idempotency_key="new")
    assert error.value.code == "MODE_DENIED" and not target.exists()


def test_disk_high_water_pauses_new_work_without_destroying_archive(tmp_path):
    store, _, _, _ = setup(tmp_path)
    store.minimum_free_bytes = 1024**4
    assert store.enqueue_due()["reason"] == "DISK_HIGH_WATER"
    assert store.status()["sources"]


class PublicDataSource:
    def __init__(self, clock): self.clock, self.calls = clock, 0
    def available(self): return {"available": True}
    def search(self, request):
        self.calls += 1
        raw = b'{"public_observation":"same"}'
        return SearchResult([{"url": "https://0.example.com/value", "title": "Official data", "text": "Complete measured sample",
            "text_kind": "data", "completeness": "complete", "published": source_time("2026-11-05T12:00:00Z")}], request.to_dict(),
            {"received_at": self.clock().isoformat(), "http_headers": {"content-type": "application/json"},
                "transport_sha256": hashlib.sha256(raw).hexdigest()},
            {"state": "public_unmetered", "requests": 1, "bytes": len(raw), "requires_reconciliation": False}, raw_bytes=raw)


def test_worker_uses_actual_service_and_restarts_with_new_cutoff_each_cycle(tmp_path):
    store, _, _, clock = setup(tmp_path / "monitor")
    adapter = PublicDataSource(clock)
    service = IntelligenceService(tmp_path / "archive", store.catalog.registry, {"source0": adapter}, clock=clock)
    worker = MonitoringWorker(store, lambda: service, owner="actual-service-worker")
    first = worker.tick()
    assert first["state"] == "completed" and first["changed"] and adapter.calls == 1
    first_plan = service.archive.plan(first["acquisition_job_id"])
    clock.advance(301)
    second = worker.tick()
    assert second["state"] == "completed" and not second["changed"] and adapter.calls == 2
    second_plan = service.archive.plan(second["acquisition_job_id"])
    assert first["acquisition_job_id"] != second["acquisition_job_id"] and first_plan["as_of"] < second_plan["as_of"]
    assert store.status()["pending_changes"][0]["revision"] == 1


def test_worker_resumes_known_complete_acquisition_without_resending(tmp_path):
    store, _, _, clock = setup(tmp_path / "monitor")
    adapter = PublicDataSource(clock)
    service = IntelligenceService(tmp_path / "archive", store.catalog.registry, {"source0": adapter}, clock=clock)
    worker = MonitoringWorker(store, lambda: service, owner="new-worker")
    store.enqueue_due(); old = store.claim("old-worker", lease_seconds=1)
    plan = worker._plan(old, service); acquired = service.create_job(plan, "watch-cycle:" + old["id"])
    store.bind_acquisition(old["id"], "old-worker", old["generation"], acquired["job_id"])
    service.collect(acquired["job_id"])
    assert adapter.calls == 1
    clock.advance(2)
    result = worker.tick()
    assert result["state"] == "completed" and adapter.calls == 1


def test_worker_never_resends_orphan_inflight_attempt(tmp_path):
    store, _, _, clock = setup(tmp_path / "monitor")
    adapter = PublicDataSource(clock)
    service = IntelligenceService(tmp_path / "archive", store.catalog.registry, {"source0": adapter}, clock=clock)
    worker = MonitoringWorker(store, lambda: service, owner="new-worker")
    store.enqueue_due(); old = store.claim("old-worker", lease_seconds=1)
    plan = worker._plan(old, service); acquired = service.create_job(plan, "watch-cycle:" + old["id"])
    store.bind_acquisition(old["id"], "old-worker", old["generation"], acquired["job_id"])
    service.archive.begin_attempt(acquired["job_id"], "source0", "search", {"parameters": {}, "reserved_requests": 1})
    clock.advance(2)
    result = worker.tick()
    assert result["state"] == "reconciliation" and adapter.calls == 0


def test_source_health_is_explicit_and_contains_no_credentials(tmp_path):
    store, _, _, _ = setup(tmp_path)
    assert store.source_health()["sources"][0]["connected"] is False
    settle(store)
    health = store.source_health()["sources"][0]
    assert health["connected"] and health["last_success"] is not None
    assert "api_key" not in json.dumps(health)


def test_monitor_event_chain_is_auditable_and_detects_tampering(tmp_path):
    store, _, _, _ = setup(tmp_path)
    settle(store)
    verified = store.verify_events()
    assert verified["events"] >= 5
    with store._connection(write=True) as connection:
        connection.execute("UPDATE monitor_events SET record='{}' WHERE sequence=(SELECT MIN(sequence) FROM monitor_events)")
    with pytest.raises(IntelligenceError) as error:
        store.verify_events()
    assert error.value.code == "HASH_MISMATCH"


def test_source_policy_changes_do_not_reuse_frozen_watch_identity(tmp_path):
    store, topic, _, clock = setup(tmp_path)
    store.enqueue_due()
    old = store.catalog.registry.get("source0")
    changed = SourceRegistry([SourceSpec("source0", "feed", "https://changed.example.com/feed", minimum_interval_seconds=0)])
    store.catalog = SourceCatalog(changed, {"sources": {"source0": {"material_types": ["news_events"], "cadence_seconds": 300}}}, reviewed_entities={})
    assert old.endpoint != changed.get("source0").endpoint and store.claim("w") is None
    assert store.status()["sources"][0]["health"] == "source_policy_changed"
    with pytest.raises(IntelligenceError):
        store.create_watch(topic["topic_id"], ["source0"], "core", "watch1")


def test_authoritative_receipt_reconciliation_releases_hold_once(tmp_path):
    store, _, _, clock = setup(tmp_path / "monitor")
    adapter = PublicDataSource(clock)
    service = IntelligenceService(tmp_path / "archive", store.catalog.registry, {"source0": adapter}, clock=clock)
    worker = MonitoringWorker(store, lambda: service, owner="w")
    store.enqueue_due(); job = store.claim("old", lease_seconds=1)
    plan = worker._plan(job, service); acquired = service.create_job(plan, "watch-cycle:" + job["id"])
    store.bind_acquisition(job["id"], "old", job["generation"], acquired["job_id"])
    attempt = service.archive.begin_attempt(acquired["job_id"], "source0", "search", {"parameters": {}, "reserved_requests": 1})
    clock.advance(2)
    result = worker.tick()
    assert result["state"] == "reconciliation"
    assert worker.reconcile(job["id"])["state"] == "reconciliation"
    service.archive.settle_attempt(acquired["job_id"], attempt, "empty", {"request_count": 1, "bytes_received": 0,
        "usage": {"state": "public_unmetered", "requires_reconciliation": False}, "observation": {"http_headers": {}, "received_at": clock().isoformat()}})
    assert worker.reconcile(job["id"])["state"] == "completed"
    assert store.status()["budget"][0]["spent"] == 1 and store.status()["budget"][0]["held"] == 0
    with pytest.raises(IntelligenceError): worker.reconcile(job["id"])


def test_observed_usage_above_hold_is_not_silently_capped(tmp_path):
    store, _, _, clock = setup(tmp_path)
    store.enqueue_due(); job = store.claim("w")
    result = store.complete(job["id"], "w", job["generation"], outcome(requests=25))
    assert result["state"] == "failed" and store.status()["budget"][0]["spent"] == 25
    assert store.status()["sources"][0]["health"] == "quota_bound_breached"
    clock.advance(301); store.enqueue_due()
    assert store.claim("w") is None


def test_byte_quota_reservation_settlement_and_unknown_hold(tmp_path):
    store, _, _, clock = setup(tmp_path, sources=2)
    store.daily_bytes = 8 * 1024 * 1024
    store.enqueue_due(); job = store.claim("one")
    assert store.claim("two") is None
    value = outcome()
    value["usage"]["bytes"] = 100
    store.complete(job["id"], "one", job["generation"], value)
    assert sum(row["bytes_spent"] for row in store.status()["budget"]) == 100
    assert sum(row["bytes_held"] for row in store.status()["budget"]) == 0
    assert store.claim("two") is None


def test_person_topic_defaults_to_original_statement_requirement(tmp_path):
    catalog = SourceCatalog(config={"entities": {"person:reviewed": {"type": "person", "name": "Reviewed author"}}})
    store = MonitoringStore(tmp_path, catalog=catalog)
    topic = store.create_topic("Confirm original remarks and contrary facts", ["person:reviewed"], idempotency_key="person")
    assert topic["material_policy"]["person_statement"]["status"] == "required"


def test_monitor_metadata_rejects_credential_like_values(tmp_path):
    store = MonitoringStore(tmp_path)
    with pytest.raises(IntelligenceError):
        store.create_topic("Question with ghp_" + "A" * 30, idempotency_key="secret")
    assert store.status()["queue"] == {}


def test_real_registered_page_conditionals_and_429_checkpoint(tmp_path, web):
    base, routes, calls = web
    routes["/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
    clock = Clock()
    spec = SourceSpec("registered_page", "page", base + "/page", allowed_hosts=("127.0.0.1",),
        minimum_interval_seconds=0, max_retries=0, options={"material_types": ["news_events"]})
    registry = SourceRegistry([spec])
    catalog = SourceCatalog(registry, reviewed_entities={})
    store = MonitoringStore(tmp_path / "monitor", catalog=catalog, clock=clock)
    topic = store.create_topic("Track original release changes", idempotency_key="topic")
    store.create_watch(topic["topic_id"], ["registered_page"], "core", "watch")
    transport = HttpTransport(allow_local=True)
    service = IntelligenceService(tmp_path / "archive", registry, registry.build_adapters(transport=transport), clock=clock)
    worker = MonitoringWorker(store, lambda: service)
    routes["/page"] = (200, "text/html", '<article><h1>Results</h1><p>Original complete operating release.</p></article>', {"ETag": '"v1"'})
    assert worker.tick()["changed"]
    assert store.status()["sources"][0]["cursor"]["etag"] == '"v1"'
    clock.advance(301)
    routes["/page"] = (304, "text/html", b"", {"ETag": '"v1"'})
    result = worker.tick()
    assert result["state"] == "completed" and not result["changed"]
    assert calls[-1]["headers"].get("If-None-Match") == '"v1"'
    assert store.status()["pending_changes"][0]["revision"] == 1
    clock.advance(301)
    routes["/page"] = (429, "text/plain", b"slow down", {"Retry-After": "7200"})
    last_success = store.status()["sources"][0]["last_success"]
    result = worker.tick()
    assert result["state"] == "failed"
    status = store.status()
    assert status["sources"][0]["next_due"] >= clock().timestamp() + 7200
    assert status["sources"][0]["last_success"] == last_success
    assert not status["queue"].get("reconciliation")
    assert sum(call["path"] == "/robots.txt" for call in calls) == 1


def test_crash_between_acquisition_creation_and_binding_reuses_frozen_plan(tmp_path):
    store, _, _, clock = setup(tmp_path / "monitor")
    adapter = PublicDataSource(clock)
    service = IntelligenceService(tmp_path / "archive", store.catalog.registry, {"source0": adapter}, clock=clock)
    worker = MonitoringWorker(store, lambda: service)
    store.enqueue_due(); old = store.claim("old", lease_seconds=1)
    plan = store.bind_plan(old["id"], "old", old["generation"], worker._plan(old, service))
    created = service.create_job(plan, "watch-cycle:" + old["id"])
    clock.advance(2)
    result = worker.tick()
    assert result["state"] == "completed" and result["acquisition_job_id"] == created["job_id"]
    assert adapter.calls == 1


def test_burst_watch_fanout_stays_within_queue_capacity(tmp_path):
    store, _, _, _ = setup(tmp_path, sources=150, max_queue=32)
    for number in range(9):
        topic = store.create_topic("Track original releases " + str(number), idempotency_key="topic-" + str(number))
        store.create_watch(topic["topic_id"], store.catalog.registry.source_ids(), "core", "watch-" + str(number))
    first = store.enqueue_due(limit=32)
    assert len(first["enqueued"]) == 32 and first["deferred_count_is_lower_bound"]
    for _ in range(4): assert store.enqueue_due(limit=32)["enqueued"] == []
    assert store.status()["queue"] == {"queued": 32}


def test_immutable_topic_scope_tampering_is_rejected(tmp_path):
    store, topic, _, _ = setup(tmp_path)
    altered = dict(topic, question="Changed question")
    with store._connection(write=True) as connection:
        connection.execute("UPDATE monitor_topics SET record=? WHERE tenant=? AND id=?", (json.dumps(altered), store.tenant, topic["topic_id"]))
    with pytest.raises(IntelligenceError) as error: store.topic(topic["topic_id"])
    assert error.value.code == "HASH_MISMATCH"


def test_extractor_configuration_and_source_withdrawal_mark_review_once(tmp_path):
    store, _, _, clock = setup(tmp_path)
    store.enqueue_due(); job = store.claim("w")
    value = outcome(); value["document_versions"][0]["extractor_config_sha256"] = "one"
    store.complete(job["id"], "w", job["generation"], value)
    clock.advance(301); store.enqueue_due(); job = store.claim("w")
    value["document_versions"][0]["extractor_config_sha256"] = "two"
    assert store.complete(job["id"], "w", job["generation"], value)["changed"]
    clock.advance(301); store.enqueue_due(); job = store.claim("w")
    gone = {"acquisition_complete": False, "document_versions": [], "usage": {"requests": 1},
        "observations": [{"http_status": 404, "error": "SOURCE_HTTP_ERROR", "delivery": "returned"}]}
    assert store.complete(job["id"], "w", job["generation"], gone)["changed"]
    revision = store.status()["pending_changes"][0]["revision"]
    assert store.status()["pending_changes"][0]["change_reason"] == "source_withdrawal_or_unavailable"
    clock.advance(301); store.enqueue_due(); job = store.claim("w")
    assert not store.complete(job["id"], "w", job["generation"], gone)["changed"]
    assert store.status()["pending_changes"][0]["revision"] == revision


def test_review_acknowledgement_cannot_erase_newer_change(tmp_path):
    store, topic, _, clock = setup(tmp_path)
    settle(store)
    ref = {"job_id": "research_known", "report_sha256": "a" * 64}
    assert store.acknowledge_changes(topic["topic_id"], 1, 1, ref)["acknowledged"]
    assert store.status()["pending_changes"] == []
    clock.advance(301); store.enqueue_due(); job = store.claim("w")
    store.complete(job["id"], "w", job["generation"], outcome("2"))
    with pytest.raises(IntelligenceError) as error:
        store.acknowledge_changes(topic["topic_id"], 1, 1, ref)
    assert error.value.code == "REVISION_CONFLICT"
    assert store.status()["pending_changes"][0]["revision"] == 2


def test_license_expiry_credentials_and_empty_allowed_uses_pause_before_claim(tmp_path):
    clock = Clock()
    reg = SourceRegistry([SourceSpec("licensed", "feed", "https://source.example.com/feed")])
    catalog = SourceCatalog(reg, {"sources": {"licensed": {"material_types": ["news_events"],
        "entitlement": {"kind": "licensed", "reference": "reviewed-subscription", "valid_until": "2026-11-07T00:00:00Z"}}}}, reviewed_entities={})
    store = MonitoringStore(tmp_path / "license", clock=clock, catalog=catalog)
    topic = store.create_topic("Original sources", idempotency_key="topic")
    store.create_watch(topic["topic_id"], ["licensed"], "core", "watch")
    clock.advance(86400 * 2); store.enqueue_due()
    assert store.claim("w") is None and store.status()["sources"][0]["health"] == "paused_entitlement"
    assert store.status()["sources"][0]["health_reason"] == "ENTITLEMENT_REQUIRED"
    assert store.source_health()["sources"][0]["health_reason"] == "ENTITLEMENT_REQUIRED"
    assert store.status()["budget"] == []
    for name, spec, override, health in [
        ("credentials", SourceSpec("source", "feed", "https://source.example.com/feed", credential_env="TEST_INTENTIONALLY_MISSING_KEY"), {}, "paused_credentials"),
        ("uses", SourceSpec("source", "feed", "https://source.example.com/feed"), {"allowed_uses": []}, "paused_allowed_uses")]:
        reg = SourceRegistry([spec]); cat = SourceCatalog(reg, {"sources": {"source": {"material_types": ["news_events"], **override}}}, reviewed_entities={})
        item = MonitoringStore(tmp_path / name, clock=clock, catalog=cat)
        topic = item.create_topic("Original sources", idempotency_key="topic")
        item.create_watch(topic["topic_id"], ["source"], "core", "watch"); item.enqueue_due()
        assert item.claim("w") is None and item.status()["sources"][0]["health"] == health
        assert item.status()["budget"] == []


@pytest.mark.parametrize("revoked,health,reason", [
    ("entitlement", "paused_entitlement", "ENTITLEMENT_REQUIRED"),
    ("credentials", "paused_credentials", "SOURCE_AUTH_REQUIRED"),
    ("uses", "paused_allowed_uses", "SOURCE_USE_DENIED")])
def test_worker_rechecks_source_rights_after_claim_before_dispatch(tmp_path, monkeypatch, revoked, health, reason):
    clock = Clock()
    monkeypatch.setenv("TEST_RUNTIME_SOURCE_KEY", "fixture-only")
    registry = SourceRegistry([SourceSpec("source0", "feed", "https://source.example.com/feed",
        credential_env="TEST_RUNTIME_SOURCE_KEY", minimum_interval_seconds=0)])
    catalog = SourceCatalog(registry, {"sources": {"source0": {"material_types": ["news_events"],
        "entitlement": {"kind": "licensed", "reference": "reviewed-license", "valid_until": "2026-11-06T00:00:01Z"}}}}, reviewed_entities={})
    store = MonitoringStore(tmp_path / "monitor", catalog=catalog, clock=clock)
    topic = store.create_topic("Original sources", idempotency_key="topic")
    store.create_watch(topic["topic_id"], ["source0"], "core", "watch")
    adapter = PublicDataSource(clock)
    service = IntelligenceService(tmp_path / "archive", registry, {"source0": adapter}, clock=clock)
    def factory():
        if revoked == "entitlement": clock.advance(2)
        elif revoked == "credentials": monkeypatch.delenv("TEST_RUNTIME_SOURCE_KEY")
        else: catalog.sources["source0"]["allowed_uses"] = []
        return service
    result = MonitoringWorker(store, factory).tick()
    assert result["state"] == "failed" and adapter.calls == 0
    status = store.status()
    assert status["sources"][0]["health"] == health and status["sources"][0]["health_reason"] == reason
    assert status["budget"][0]["held"] == status["budget"][0]["spent"] == 0
    assert status["sources"][0]["last_success"] is None


def test_same_scope_can_be_recreated_under_new_catalog_without_changing_question(tmp_path):
    store, original, _, _ = setup(tmp_path)
    registry = store.catalog.registry
    store.catalog = SourceCatalog(registry, {"version": "new-reviewed-contract", "sources": {
        "source0": {"material_types": ["news_events"], "cadence_seconds": 300}}}, reviewed_entities={})
    current = store.create_topic(original["question"], idempotency_key="new-contract")
    assert current["topic_id"] != original["topic_id"] and current["scope_hash"] == original["scope_hash"]
    assert current["catalog_sha256"] == store.catalog.manifest()["sha256"]
    assert store.create_topic(original["question"], idempotency_key="topic1") == original
    assert store.topic(original["topic_id"]) == original and store.topic(current["topic_id"]) == current
    with pytest.raises(IntelligenceError) as error:
        store.create_watch(original["topic_id"], ["source0"], "core", "new-watch-old-topic")
    assert error.value.code == "PLAN_CONFLICT"
    assert store.create_watch(current["topic_id"], ["source0"], "core", "new-watch-current-topic")["catalog_sha256"] == current["catalog_sha256"]


def test_legacy_scope_only_topic_identity_remains_readable(tmp_path):
    from trading_system.intelligence.contracts import digest
    store, topic, _, _ = setup(tmp_path)
    scope = {key: topic[key] for key in ("question", "entity_ids", "horizon", "material_policy")}
    legacy = {**topic, "topic_id": "topic_" + digest({"tenant": store.tenant, "scope": scope})[:32]}
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO monitor_topics VALUES(?,?,?,?,?)", (store.tenant,
            legacy["topic_id"], 1, legacy["scope_hash"], json.dumps(legacy)))
    assert store.topic(legacy["topic_id"]) == legacy


def test_bounded_twenty_article_feed_collects_every_original_in_one_monitor_unit(tmp_path, web):
    base, routes, calls = web
    routes["/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
    clock = Clock()
    registry = SourceRegistry([SourceSpec("official_feed", "feed", base + "/releases.xml",
        allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0, max_retries=0)])
    catalog = SourceCatalog(registry, {"sources": {"official_feed": {"material_types": ["issuer_ir"],
        "cadence_seconds": 300}}}, reviewed_entities={})
    store = MonitoringStore(tmp_path / "monitor", clock=clock, catalog=catalog)
    topic = store.create_topic("Track the complete finite original release window", idempotency_key="topic")
    store.create_watch(topic["topic_id"], ["official_feed"], "core", "watch")
    items = ''.join('<item><title>Release ' + str(i) + '</title><link>' + base + '/release-' + str(i)
        + '</link><pubDate>Thu, 05 Nov 2026 12:00:00 GMT</pubDate><description>Original release discovery</description></item>' for i in range(20))
    routes["/releases.xml"] = (200, "application/rss+xml", '<rss><channel>' + items + '</channel></rss>')
    for index in range(20):
        routes['/release-' + str(index)] = (200, "text/html", '<article><h1>Original release</h1><p>Complete original operating release ' + str(index) + '</p></article>')
    service = IntelligenceService(tmp_path / "archive", registry,
        registry.build_adapters(transport=HttpTransport(allow_local=True)), clock=clock)
    result = MonitoringWorker(store, lambda: service).tick()
    assert result["state"] == "completed" and len(calls) == 22 and len({call["path"] for call in calls}) == 22
    assert calls[0]["path"] == "/robots.txt"
    plan = service.archive.plan(result["acquisition_job_id"])
    assert plan["limits"]["fetches"] == 24 and plan["limits"]["requests"] == 48
    assert plan["limits"]["deadline_seconds"] == 240 and plan["limits"]["bytes"] == 8 * 1024 * 1024
    evidence = service.evidence(job_id=result["acquisition_job_id"])
    assert len([doc for doc in evidence["documents"] if doc["text_kind"] == "html_main" and doc["completeness"] == "complete"]) == 20
    assert store.status()["budget"][0]["spent"] == 22 and store.status()["budget"][0]["held"] == 0
    assert store.status()["sources"][0]["health"] == "healthy"
