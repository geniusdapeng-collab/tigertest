"""Snapshot-only keeps full decision scope and cannot dispatch a source.

The origin fixture uses explicit localhost HTTP to create new-format originals;
the target snapshot job must make zero source calls, even with ready adapters.
"""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from trading_system.intelligence import IntelligenceError, IntelligenceService, SourceRegistry, SourceSpec
from trading_system.intelligence.contracts import canonical, digest
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.replay import verify_bundle
from test_intelligence_acquisition import NOW, plan as original_plan, service as origin_service, web


def registry():
    return SourceRegistry([
        SourceSpec("web_page", "web_fetch", "", minimum_interval_seconds=0),
        SourceSpec("deepseek", "search", "https://api.deepseek.com/v1/tools/search", minimum_interval_seconds=0, max_retries=0),
        SourceSpec("official", "page", "https://example.test/disclosure", allowed_hosts=("example.test",),
            authority_scopes=("issuer_ir",), minimum_interval_seconds=0, options={"material_types": ["issuer_financial"]}),
    ])


def request(**changes):
    value = {"run_id": "snapshot-core", "as_of": "2026-11-05T13:00:00+00:00", "market": "us",
        "universe": {"AAPL": "listed:US:AAPL", "MU": "listed:US:MU", "NVDA": "listed:US:NVDA"},
        "frontier": ["AAPL", "MU"], "holdings": ["AAPL", "UNKNOWN"], "pending": ["MU", "ZZZ"],
        "watchlist": ["NVDA"], "frontier_manifest_hash": "a" * 64, "market_snapshot_refs": []}
    return {**value, **changes}


class ForbiddenAdapter:
    credential = None

    def search(self, *_args, **_kwargs):
        pytest.fail("Snapshot-only dispatched a source search")

    def fetch(self, *_args, **_kwargs):
        pytest.fail("Snapshot-only dispatched a source fetch")


def target_service(root, reg):
    return IntelligenceService(root, reg, {source: ForbiddenAdapter() for source in reg.source_ids()},
        clock=lambda: NOW, observe_io=lambda *_args: pytest.fail("Snapshot-only observed provider I/O"))


def create_empty(tmp_path, **changes):
    reg = registry()
    service = target_service(tmp_path, reg)
    plan = build_plan(request(acquisition_mode="snapshot_only", baseline_refs=[{"manifest_sha256": "b" * 64}], **changes), reg)
    job_id = service.create_job(plan, "snapshot-empty")["job_id"]
    return service, plan, job_id


@pytest.fixture
def imported(tmp_path, web):
    origin, reg, _ = origin_service(tmp_path / "origin", web)
    old_plan = original_plan(reg)
    old_job = origin.create_job(old_plan, "origin-fixture")["job_id"]
    original = origin.collect(old_job)
    assert len(original["documents"]) == 2 and len(web[2]) == 2
    manifest = origin.archive_versions("2026-11-05T13:00:00Z")
    service = target_service(tmp_path / "target", reg)
    cells = [{"cell_id": key, "target_id": "security:listed:US:AAPL" if key == "present" else "chain:" + key,
        "entity_id": "listed:US:AAPL", "question": "Question " + key, "required": True}
        for key in ("present", "missing", "conflicting")]
    cells.append({"cell_id": "connection", "target_id": "account:holdings", "question": "Holdings connection",
        "required": False, "material_type": "connection"})
    plan = original_plan(reg, run_id="snapshot-target", acquisition_mode="snapshot_only", required_cells=cells,
        baseline_refs=[{"manifest_sha256": manifest["manifest_sha256"]}])
    job_id = service.create_job(plan, "snapshot-imported")["job_id"]
    receipt = service.import_baseline(job_id, origin.archive.root,
        version_ids=[row["id"] for row in manifest["versions"]], expected_manifest_hash=manifest["manifest_sha256"])
    return service, plan, job_id, origin, manifest, receipt, web


def assert_zero_source_calls(service, job_id):
    status = service.status(job_id)
    assert status["attempts"] == []
    assert status["usage"] == {"requests": 0, "fetches": 0, "bytes": 0, "unreconciled_attempts": 0}
    assert not service.archive.events(job_id, kind="attempt_started")


def test_default_and_explicit_live_preserve_exact_pre_extension_plan_bytes():
    missing = build_plan(request(), registry())
    live = build_plan(request(acquisition_mode="live"), registry())
    # Independently computed from root's pre-extension 7c0f source, before this
    # candidate changed planner.py. Protect the complete serialized contract.
    assert missing["plan_hash"] == "b6e040c7b64c6945578f7e7786b5ab6f2e483e52c695ed0fa51d441c441da8ac"
    assert digest(missing) == "f9b002620b1969fb12ac05236211185053142239906708c1cd973543eb82d6ae"
    assert canonical(live) == canonical(missing) and "acquisition_mode" not in missing


def test_snapshot_plan_retains_every_coverage_roster_registry_and_budget_field():
    live = build_plan(request(), registry())
    snapshot = build_plan(request(acquisition_mode="snapshot_only"), registry())
    excluded = {"query_plan", "plan_hash", "acquisition_mode"}
    assert {key: value for key, value in snapshot.items() if key not in excluded} == {
        key: value for key, value in live.items() if key not in excluded}
    assert len(snapshot["coverage_cells"]) == len(live["coverage_cells"]) == 30
    assert snapshot["frontier"] == ["AAPL", "MU", "NVDA"]
    assert snapshot["unavailable_holdings"] == ["UNKNOWN"] and snapshot["unavailable_pending"] == ["ZZZ"]
    assert snapshot["query_plan"] == [] and live["query_plan"]
    assert snapshot["acquisition_mode"] == "snapshot_only"
    assert snapshot["plan_hash"] == digest({key: value for key, value in snapshot.items() if key != "plan_hash"})
    assert snapshot["plan_hash"] != live["plan_hash"]


@pytest.mark.parametrize("mode", [None, "", "Snapshot_only", "offline", 0, True, [], {}])
def test_unknown_or_wrong_typed_mode_is_rejected(mode):
    with pytest.raises(IntelligenceError) as caught:
        build_plan(request(acquisition_mode=mode), registry())
    assert caught.value.code == "CONTRACT_INVALID"


@pytest.mark.parametrize("operation", ["search", "fetch"])
def test_call_guard_precedes_adapter_budget_attempt_and_observation(tmp_path, monkeypatch, operation):
    service, _plan, job_id = create_empty(tmp_path)
    before = service.status(job_id)
    denied = lambda *_args, **_kwargs: pytest.fail("Snapshot-only crossed its pre-dispatch guard")
    monkeypatch.setattr(service.registry, "get", denied)
    monkeypatch.setattr(service.archive, "begin_attempt", denied)
    monkeypatch.setattr(service, "_deadline", denied)
    with pytest.raises(IntelligenceError) as caught:
        service._call(job_id, "deepseek", operation, {"url": "https://example.test/original"})
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details == {"reason": "SNAPSHOT_ONLY_NO_LIVE_ACQUISITION"}
    assert service.status(job_id) == before
    assert_zero_source_calls(service, job_id)


def test_empty_snapshot_collect_fails_closed_without_changing_coverage_or_dispatch(tmp_path, monkeypatch):
    service, plan, job_id = create_empty(tmp_path)
    monkeypatch.setattr(service, "_query", lambda *_args: pytest.fail("Snapshot-only compiled/dispatched a query"))
    before = service.status(job_id)
    with pytest.raises(IntelligenceError) as caught:
        service.collect(job_id)
    assert caught.value.code == "POLICY_DENIED"
    assert service.status(job_id) == before
    assert {row["cell_id"] for row in before["gaps"]} == {row["cell_id"] for row in plan["coverage_cells"]}
    assert_zero_source_calls(service, job_id)
    assert not service.archive.events(job_id, kind="snapshot_collection_completed")
    with pytest.raises(IntelligenceError) as caught:
        service.research(job_id, replay_key="no-imported-versions")
    assert caught.value.code == "POLICY_DENIED" and service.status(job_id) == before


@pytest.mark.parametrize("refs", [[], None, [{}], ["not-a-ref"], [{"manifest_sha256": None}], [{"manifest_sha256": "bad"}]])
def test_snapshot_service_requires_a_nonempty_typed_baseline_binding(tmp_path, refs):
    reg = registry()
    service = target_service(tmp_path, reg)
    plan = build_plan(request(acquisition_mode="snapshot_only", baseline_refs=refs), reg)
    with pytest.raises(IntelligenceError) as caught:
        service.create_job(plan, "missing-baseline")
    assert caught.value.code == "POLICY_DENIED"


def test_empty_version_import_cannot_be_collected_researched_or_frozen(imported):
    service, original_plan, _job_id, origin, manifest, _receipt, web = imported
    target = target_service(service.archive.root.parent / "empty-import-target", service.registry)
    job_id = target.create_job(original_plan, "empty-versions")["job_id"]
    receipt = target.import_baseline(job_id, origin.archive.root, version_ids=[],
        expected_manifest_hash=manifest["manifest_sha256"])
    assert receipt["versions"] == [] and target.archive.documents(job_id) == []
    before = target.status(job_id)
    count = len(web[2])
    operations = [lambda: target.collect(job_id), lambda: target.research(job_id),
        lambda: target.freeze(job_id, expected_plan_hash=original_plan["plan_hash"],
            expected_revision=before["revision"], expected_frontier_hash=original_plan["frontier_manifest_hash"])]
    for operation in operations:
        with pytest.raises(IntelligenceError) as caught:
            operation()
        assert caught.value.code == "POLICY_DENIED" and target.status(job_id) == before
    assert len(web[2]) == count
    assert_zero_source_calls(target, job_id)


def test_snapshot_freeze_requires_verified_imports_before_any_seal(tmp_path):
    service, plan, job_id = create_empty(tmp_path)
    before = service.status(job_id)
    with pytest.raises(IntelligenceError) as caught:
        service.freeze(job_id, expected_plan_hash=plan["plan_hash"], expected_revision=before["revision"],
            expected_frontier_hash=plan["frontier_manifest_hash"])
    assert caught.value.code == "POLICY_DENIED" and service.status(job_id) == before
    assert not service.archive.events(job_id, kind="bundle_frozen")
    assert_zero_source_calls(service, job_id)


def test_collect_uses_only_imported_originals_without_semantic_reuse_or_new_io(imported, monkeypatch):
    service, plan, job_id, origin, manifest, receipt, web = imported
    original_count = len(web[2])
    monkeypatch.setattr(service, "_query", lambda *_args: pytest.fail("Snapshot-only dispatched a query"))
    acquired = service.collect(job_id)
    assert len(web[2]) == original_count
    assert {row["id"] for row in acquired["documents"]} == {row["id"] for row in manifest["versions"]}
    for document in acquired["documents"]:
        assert document["text"] == origin.archive.document(document["id"])["text"]
        assert document["times"]["first_seen"]["value"] == NOW.isoformat()
    assert receipt["reused_semantic_annotations"] is False and receipt["reused_search_results"] is False
    assert all(row["status"] != "present" for row in acquired["gaps"])
    assert any(row["reason"] == "SEMANTIC_ASSESSMENT_REQUIRED" for row in acquired["gaps"])
    assert service.collect(job_id) == acquired
    checkpoint = service.archive.events(job_id, kind="snapshot_collection_completed")
    assert len(checkpoint) == 1 and checkpoint[0]["payload"]["mode"] == "snapshot_only"
    assert checkpoint[0]["payload"]["documents_sha256"] == digest(acquired["documents"])
    assert service.archive.plan(job_id) == plan
    assert_zero_source_calls(service, job_id)


def test_research_preserves_actual_present_conflict_connection_and_audits_unavailable(imported, monkeypatch):
    service, plan, job_id, _origin, _manifest, _receipt, web = imported
    acquired = service.collect(job_id)
    original = next(row for row in acquired["documents"] if row["text_kind"] == "html_main")
    service.record_assessment(job_id, [], [
        {"cell_id": "present", "status": "present", "document_ids": [original["id"]]},
        {"cell_id": "missing", "status": "missing", "reason": "no_qualified_material_for_this_question"},
        {"cell_id": "conflicting", "status": "conflicting", "document_ids": [original["id"]], "reason": "unresolved_sources"},
    ])
    original_count = len(web[2])
    monkeypatch.setattr(service, "_query", lambda *_args: pytest.fail("Snapshot-only research dispatched"))
    result = service.research(job_id, replay_key="snapshot-round")
    gaps = {row["cell_id"]: row for row in result["gaps"]}
    assert gaps["present"]["status"] == "present"
    assert gaps["conflicting"]["status"] == "conflicting" and gaps["conflicting"]["reason"] == "unresolved_sources"
    assert gaps["connection"]["status"] == "not_connected"
    assert gaps["missing"]["status"] == "unverifiable"
    assert gaps["missing"]["reason"] == "SNAPSHOT_ONLY_NO_LIVE_ACQUISITION"
    assert len(web[2]) == original_count
    assert_zero_source_calls(service, job_id)
    started = service.archive.events(job_id, kind="research_started")
    unavailable = service.archive.events(job_id, kind="research_unavailable")
    completed = service.archive.events(job_id, kind="research_completed")
    assert len(started) == len(unavailable) == len(completed) == 1
    assert started[0]["payload"]["query_plan"] == [] and started[0]["payload"]["mode"] == "snapshot_only"
    assert unavailable[0]["payload"]["request_count"] == unavailable[0]["payload"]["bytes_received"] == 0
    assert completed[0]["payload"]["state"] == "unavailable" and completed[0]["payload"]["mode"] == "snapshot_only"
    assert service.research(job_id, replay_key="snapshot-round") == result
    assert service.archive.plan(job_id) == plan
    with pytest.raises(IntelligenceError) as caught:
        service.research(job_id, gap_ids=["missing"], replay_key="snapshot-round")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


def test_round_quota_unknown_gap_and_expired_deadline_remain_visible(imported):
    service, _plan, job_id, _origin, _manifest, _receipt, _web = imported
    service.collect(job_id)
    with pytest.raises(IntelligenceError) as caught:
        service.research(job_id, gap_ids=["not-in-plan"])
    assert caught.value.code == "CONTRACT_INVALID"
    service.research(job_id, replay_key="round-two")
    service.research(job_id, replay_key="round-three")
    with pytest.raises(IntelligenceError) as caught:
        service.research(job_id, replay_key="round-four")
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert_zero_source_calls(service, job_id)


def test_expired_snapshot_collect_and_research_do_not_reset_budget_or_dispatch(imported):
    service, plan, job_id, _origin, _manifest, _receipt, _web = imported
    service.clock = lambda: NOW + timedelta(seconds=plan["limits"]["deadline_seconds"] + 1)
    acquired = service.collect(job_id)
    assert all(row["reason"] == "BUDGET_EXHAUSTED" for row in acquired["gaps"]
               if row["target_id"].startswith(("global:", "chain:", "sector:", "security:")))
    assert service.archive.events(job_id, kind="snapshot_collection_completed")[-1]["payload"]["state"] == "budget_exhausted"
    service.research(job_id, replay_key="expired-round")
    assert service.archive.events(job_id, kind="research_completed")[-1]["payload"]["state"] == "budget_exhausted"
    assert service.archive.events(job_id, kind="research_unavailable")[-1]["payload"]["reason"] == "BUDGET_EXHAUSTED"
    assert service.archive.plan(job_id)["limits"] == plan["limits"]
    assert_zero_source_calls(service, job_id)


@pytest.mark.parametrize("mode", ["unknown", "live", None, [], {}])
def test_even_rehashed_unknown_stored_mode_is_rejected_before_job_creation(tmp_path, mode):
    service, plan, _job_id = create_empty(tmp_path)
    plan["acquisition_mode"] = mode
    plan["plan_hash"] = digest({key: value for key, value in plan.items() if key != "plan_hash"})
    with pytest.raises(IntelligenceError) as caught:
        service.create_job(plan, "invalid-mode")
    assert caught.value.code == "CONTRACT_INVALID"


def test_rehashed_snapshot_query_plan_cannot_bypass_service_guard(tmp_path):
    service, plan, _job_id = create_empty(tmp_path)
    plan["query_plan"] = build_plan(request(), service.registry)["query_plan"]
    plan["plan_hash"] = digest({key: value for key, value in plan.items() if key != "plan_hash"})
    with pytest.raises(IntelligenceError) as caught:
        service.create_job(plan, "bad-query-plan")
    assert caught.value.code == "PLAN_CONFLICT"
    # Archive is a lower-level trusted storage API; even a caller using it
    # directly cannot make service collect/research dispatch an invalid plan.
    job_id = service.archive.create_job(plan, "direct-storage")["job_id"]
    for operation in (service.collect, service.research):
        with pytest.raises(IntelligenceError) as caught:
            operation(job_id)
        assert caught.value.code == "PLAN_CONFLICT"
    assert_zero_source_calls(service, job_id)


def test_plan_byte_tamper_and_live_replay_key_conflict_are_not_silent(tmp_path):
    service, plan, job_id = create_empty(tmp_path)
    with pytest.raises(IntelligenceError) as caught:
        service.create_job(build_plan(request(), service.registry), "snapshot-empty")
    assert caught.value.code == "PLAN_CONFLICT"
    altered = {**plan, "acquisition_mode": "live"}
    with sqlite3.connect(service.archive.database) as connection:
        connection.execute("UPDATE jobs SET plan_json=? WHERE job_id=?", (canonical(altered).decode(), job_id))
    with pytest.raises(IntelligenceError) as caught:
        service.collect(job_id)
    assert caught.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("change", ["missing_manifest", "unadmitted_manifest", "semantic_reuse", "version_binding"])
def test_snapshot_collect_rejects_invalid_import_receipt(imported, change):
    service, _plan, job_id, _origin, _manifest, receipt, _web = imported
    invalid = deepcopy(receipt)
    if change == "missing_manifest":
        invalid.pop("manifest")
    elif change == "unadmitted_manifest":
        invalid["manifest_sha256"] = "f" * 64
    elif change == "semantic_reuse":
        invalid["reused_semantic_annotations"] = True
    else:
        invalid["versions"][0]["document_sha256"] = "f" * 64
    service.archive.append(job_id, "baseline_import", invalid)
    with pytest.raises(IntelligenceError) as caught:
        service.collect(job_id)
    assert caught.value.code == "HASH_MISMATCH"
    assert_zero_source_calls(service, job_id)


def test_unimported_original_and_modified_blob_cannot_enter_snapshot_collection(imported):
    service, _plan, job_id, _origin, _manifest, _receipt, _web = imported
    record = service.archive.document(service.archive.documents(job_id)[0]["id"])
    injected = {**record, "id": "docver_" + "f" * 32, "version": "f" * 64}
    service.archive.put_document(job_id, injected, attempt_id="not-an-import", preserve_record=True)
    with pytest.raises(IntelligenceError) as caught:
        service.collect(job_id)
    assert caught.value.code == "HASH_MISMATCH"
    assert_zero_source_calls(service, job_id)


def test_imported_raw_blob_tamper_is_rejected_before_checkpoint(imported):
    service, _plan, job_id, _origin, _manifest, _receipt, _web = imported
    document = service.archive.documents(job_id)[0]
    (service.archive.root / document["raw_blob"]["path"]).write_bytes(b"tampered original")
    with pytest.raises(IntelligenceError) as caught:
        service.collect(job_id)
    assert caught.value.code == "HASH_MISMATCH"
    assert not service.archive.events(job_id, kind="snapshot_collection_completed")


def test_frozen_snapshot_bundle_replays_originals_offline_and_cannot_reacquire(imported):
    service, plan, job_id, _origin, _manifest, _receipt, web = imported
    acquired = service.collect(job_id)
    service.research(job_id, replay_key="before-freeze")
    bundle = service.freeze(job_id, expected_plan_hash=plan["plan_hash"],
        expected_revision=service.status(job_id)["revision"], expected_frontier_hash=plan["frontier_manifest_hash"])
    assert bundle["state"] == "partial" and len(bundle["documents"]) == 2
    count = len(web[2])
    assert verify_bundle(service.archive.root, bundle["bundle_id"], expected_plan_hash=plan["plan_hash"],
        expected_registry_sha256=service.registry.public_manifest()["sha256"]) == bundle
    assert len(web[2]) == count and bundle["usage"] == acquired["usage"]
    for operation in (service.collect, service.research):
        with pytest.raises(IntelligenceError) as caught:
            operation(job_id)
        assert caught.value.code == "FROZEN_BUNDLE"
    assert_zero_source_calls(service, job_id)


def test_material_outside_imported_members_stays_missing_without_scope_reduction(imported):
    service, _plan, _job_id, origin, manifest, _receipt, web = imported
    reg = service.registry
    target = target_service(service.archive.root.parent / "other-target", reg)
    plan = original_plan(reg, acquisition_mode="snapshot_only", run_id="uncovered-security",
        universe={"AAPL": "listed:US:AAPL", "MU": "listed:US:MU"}, frontier=["AAPL", "MU"],
        baseline_refs=[{"manifest_sha256": manifest["manifest_sha256"]}], required_cells=[
            {"cell_id": "aapl", "target_id": "security:listed:US:AAPL", "entity_id": "listed:US:AAPL", "question": "AAPL baseline", "required": True},
            {"cell_id": "mu", "target_id": "security:listed:US:MU", "entity_id": "listed:US:MU", "question": "MU baseline", "required": True}])
    job_id = target.create_job(plan, "uncovered-material")["job_id"]
    target.import_baseline(job_id, origin.archive.root, version_ids=[row["id"] for row in manifest["versions"]],
        expected_manifest_hash=manifest["manifest_sha256"])
    count = len(web[2])
    acquired = target.collect(job_id)
    gaps = {row["cell_id"]: row for row in acquired["gaps"]}
    assert gaps["aapl"]["status"] == "pending"
    assert gaps["mu"]["status"] == "missing" and gaps["mu"]["reason"] == "SNAPSHOT_ONLY_NO_IMPORTED_MATERIAL"
    assert target.archive.plan(job_id)["frontier"] == ["AAPL", "MU"] and len(gaps) == 2
    assert len(web[2]) == count
    assert_zero_source_calls(target, job_id)


def test_checkpoint_detects_late_association_even_when_document_ids_do_not_change(imported):
    service, _plan, job_id, _origin, _manifest, _receipt, _web = imported
    acquired = service.collect(job_id)
    record = service.archive.document(acquired["documents"][0]["id"])
    service.archive.put_document(job_id, record, attempt_id="late-association", preserve_record=True,
        targets=["chain:missing"], entities=["listed:US:AAPL"])
    with pytest.raises(IntelligenceError) as caught:
        service.collect(job_id)
    assert caught.value.code == "PLAN_CONFLICT"
    assert_zero_source_calls(service, job_id)


@pytest.mark.parametrize("change", ["query_plan", "mode"])
def test_resumed_snapshot_research_rejects_a_foreign_dispatch_or_mode(imported, change):
    service, _plan, job_id, _origin, _manifest, _receipt, _web = imported
    acquired = service.collect(job_id)
    frozen = {"round": 2, "gap_ids": [row["cell_id"] for row in acquired["gaps"]], "gaps": acquired["gaps"],
        "replay_key": "invalid-resume", "requested_gap_ids": None, "query_plan": [], "mode": "snapshot_only"}
    frozen[change] = [{}] if change == "query_plan" else "live"
    service.archive.append(job_id, "research_started", frozen)
    with pytest.raises(IntelligenceError) as caught:
        service.research(job_id, replay_key="invalid-resume")
    assert caught.value.code == "PLAN_CONFLICT"
    assert_zero_source_calls(service, job_id)
