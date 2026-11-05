"""Acquisition CLI boundary, with an in-memory service and no external I/O."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys

import pytest

from trading_system.intelligence.contracts import IntelligenceError, digest
from trading_system.intelligence_cli import execute, read_request, validate_request
from trading_system.intelligence.registry import default_registry

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


@pytest.fixture
def intel_request():
    return {"schema": "tiger.intelligence-request/v1", "request_id": "operator-001",
            "as_of": "2026-11-04T20:00:00Z", "symbols": ["MSFT", "AAPL"],
            "source_ids": ["fed_policy"]}


class FakeService:
    def __init__(self):
        self.readonly = None
        self.jobs = {}
        self.calls = []

    def factory(self, root, registry, *, readonly):
        self.readonly = readonly
        self.calls.append(("factory", str(root)))
        return self

    def create_job(self, plan, replay_key):
        self.calls.append(("create", plan, replay_key))
        key = "intel_" + digest(replay_key)[:32]
        if key not in self.jobs:
            self.jobs[key] = {"job_id": key, "run_id": plan["run_id"], "state": "accepted", "revision": 1,
                              "plan_hash": plan["plan_hash"], "bundle": None, "gaps": []}
        elif self.jobs[key]["plan_hash"] != plan["plan_hash"]:
            raise IntelligenceError("PLAN_CONFLICT")
        return dict(self.jobs[key])

    def collect(self, job_id):
        assert not self.readonly
        self.calls.append(("collect", job_id))
        self.jobs[job_id].update(state="partial", revision=2)

    def status(self, job_id):
        self.calls.append(("status", job_id))
        return dict(self.jobs[job_id]) if job_id in self.jobs else {"job_id": job_id, "state": "partial"}

    def freeze(self, job_id, *, expected_plan_hash, expected_revision, expected_frontier_hash):
        self.calls.append(("freeze", job_id, expected_plan_hash, expected_revision, expected_frontier_hash))
        assert expected_revision == 2
        assert expected_plan_hash == self.jobs[job_id]["plan_hash"]
        result = {"bundle_id": "bundle_" + "a" * 32, "manifest_sha256": "b" * 64}
        self.jobs[job_id]["bundle"] = result
        return result

    def evidence(self, **intel_request):
        self.calls.append(("evidence", intel_request))
        return {"verified": True, **intel_request}


def plan_builder(value, registry):
    return {**value, "plan_hash": digest(value)}


def test_sync_is_bounded_unassessed_and_has_no_account(tmp_path, intel_request):
    service = FakeService()
    result = execute("sync", intel_request, archive_root=tmp_path / "archive", mode="acquire",
                     service_factory=service.factory, plan_builder=plan_builder, now=NOW)
    assert result["operation_ok"]
    assert result["data"]["semantics"] == "acquisition_only_unassessed"
    assert result["data"]["account_connected"] is False
    assert not (tmp_path / "archive").exists()
    plan = next(x[1] for x in service.calls if x[0] == "create")
    assert plan["frontier"] == ["AAPL", "MSFT"]
    assert plan["holdings"] is None
    assert plan["market_snapshot_refs"] == []
    assert plan["limits"] == {"requests": 24, "fetches": 12, "bytes": 8388608,
                              "rounds": 1, "concurrency": 1, "deadline_seconds": 60}
    assert len([x for x in service.calls if x[0] == "freeze"]) == 1


def test_replay_same_request_does_not_recollect_frozen_bundle(tmp_path, intel_request):
    service = FakeService()
    for _ in range(2):
        execute("sync", intel_request, archive_root=tmp_path, mode="acquire", service_factory=service.factory,
                plan_builder=plan_builder, now=NOW)
    assert len([x for x in service.calls if x[0] == "collect"]) == 1
    assert len([x for x in service.calls if x[0] == "freeze"]) == 1


def test_changed_request_with_same_key_is_conflict(tmp_path, intel_request):
    service = FakeService()
    execute("sync", intel_request, archive_root=tmp_path, mode="acquire", service_factory=service.factory,
            plan_builder=plan_builder, now=NOW)
    intel_request["symbols"] = ["NVDA"]
    with pytest.raises(IntelligenceError, match="PLAN_CONFLICT"):
        execute("sync", intel_request, archive_root=tmp_path, mode="acquire", service_factory=service.factory,
                plan_builder=plan_builder, now=NOW)


def test_monitor_has_fixed_cutoff_new_cycle_keys_and_bounded_count(tmp_path, intel_request):
    service = FakeService()
    waits = []
    result = execute("monitor", intel_request, archive_root=tmp_path, mode="acquire", cycles=3, interval_seconds=2,
                     service_factory=service.factory, plan_builder=plan_builder, monotonic=lambda: 0,
                     sleep=waits.append, now=NOW)
    created = [x for x in service.calls if x[0] == "create"]
    assert len(created) == 3
    assert len({x[2] for x in created}) == 3
    assert {x[1]["as_of"] for x in created} == {"2026-11-04T20:00:00+00:00"}
    assert waits == [2, 2]
    assert result["data"]["cutoff_advanced"] is False


def test_monitor_does_not_admit_cycle_without_full_tool_deadline(tmp_path, intel_request):
    service = FakeService()
    ticks = iter([0, 0, 90, 90])
    result = execute("monitor", intel_request, archive_root=tmp_path, mode="acquire", cycles=3, wall_seconds=120,
                     service_factory=service.factory, plan_builder=plan_builder, monotonic=lambda: next(ticks),
                     sleep=lambda _: None, now=NOW)
    assert result["data"]["cycles_completed"] == 1
    assert result["data"]["stopped_reason"] == "WALL_DEADLINE"


def test_readonly_rejects_before_constructing_archive(tmp_path, intel_request):
    with pytest.raises(IntelligenceError, match="READ_ONLY"):
        execute("sync", intel_request, archive_root=tmp_path / "absent", service_factory=lambda *a, **k: pytest.fail("factory called"))
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("operation,input", [
    ("status", {"job_id": "intel_" + "a" * 32}),
    ("evidence", {"document_id": "doc_abc@version"}),
    ("evidence", {"bundle_id": "bundle_" + "a" * 32, "claim_id": "event:claim1"}),
])
def test_queries_construct_readonly_service(tmp_path, operation, input):
    service = FakeService()
    result = execute(operation, input, archive_root=tmp_path, service_factory=service.factory)
    assert result["operation_ok"]
    assert service.readonly is True
    assert not any(x[0] in {"create", "collect", "freeze"} for x in service.calls)


@pytest.mark.parametrize("field,value", [
    ("archive_root", "/tmp/injected"), ("url", "https://example.com"),
    ("query", "free-form injected query"), ("env", {"LLM_API_KEY": "injected"}),
    ("account", "live"), ("limits", {"requests": 100000}), ("model", "other"),
])
def test_request_cannot_override_trusted_controls(tmp_path, intel_request, field, value):
    intel_request[field] = value
    with pytest.raises(IntelligenceError, match="INVALID_REQUEST"):
        execute("sync", intel_request, archive_root=tmp_path, mode="acquire", now=NOW,
                service_factory=lambda *a, **k: pytest.fail("invalid intel_request constructed service"))


@pytest.mark.parametrize("change", [
    {"symbols": ["AAPL", "AAPL"]}, {"symbols": ["005930.KS"]}, {"symbols": []},
    {"symbols": ["aapl"]}, {"source_ids": ["unknown-source"]}, {"source_ids": ["brave"]},
    {"source_ids": []}, {"as_of": "2026-11-04"}, {"as_of": "2099-01-01T00:00:00Z"},
    {"request_id": "../escape"},
])
def test_invalid_symbols_sources_time_and_keys_are_rejected(intel_request, change):
    intel_request.update(change)
    with pytest.raises(IntelligenceError):
        validate_request("sync", intel_request, default_registry(), now=NOW)


def test_optional_provider_requires_trusted_startup_enable(tmp_path, intel_request):
    intel_request["source_ids"] = ["brave"]
    service = FakeService()
    assert execute("sync", intel_request, archive_root=tmp_path, mode="acquire", enabled_sources=["brave"],
                   service_factory=service.factory, plan_builder=plan_builder, now=NOW)["operation_ok"]


@pytest.mark.parametrize("intel_request", [
    {"job_id": "../secret"}, {"job_id": "/tmp/secret"}, {"job_id": "id", "root": "/tmp"},
])
def test_query_paths_and_unknown_fields_rejected(intel_request):
    with pytest.raises(IntelligenceError):
        validate_request("status", intel_request, default_registry())


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'[]', b'\xff'])
def test_bounded_json_is_exact_and_finite(raw):
    with pytest.raises(IntelligenceError):
        read_request(BytesIO(raw))


def test_oversized_json_rejected():
    with pytest.raises(IntelligenceError, match="INPUT_TOO_LARGE"):
        read_request(BytesIO(b" " * 65537))


def test_python_readonly_cli_is_safe_without_service_or_archive(tmp_path):
    root = tmp_path / "archive"
    proc = subprocess.run([sys.executable, "-m", "trading_system.intelligence_cli", "sync", "--archive-root", str(root)],
                          input="malformed on purpose", capture_output=True, text=True, timeout=10)
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["error"]["code"] == "READ_ONLY"
    assert not root.exists()


def test_real_service_missing_identity_freezes_explicit_gaps_and_readonly_verifies(tmp_path, intel_request):
    from trading_system.intelligence import IntelligenceService, verify_bundle
    from trading_system.intelligence.transport import HttpTransport

    class DenyTransport(HttpTransport):
        def __init__(self):
            super().__init__()
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("No network is authorized in this offline integration")

    denied = DenyTransport()
    archive = tmp_path / "actual-archive"

    def factory(root, registry, *, readonly):
        return IntelligenceService(root, registry, {} if readonly else registry.build_adapters(
            environment={}, transport=denied), readonly=readonly, clock=lambda: NOW)

    intel_request["source_ids"] = ["sec_directory", "sec_current", "sec_submissions"]
    result = execute("sync", intel_request, archive_root=archive, mode="acquire", service_factory=factory, now=NOW)
    cycle = result["data"]["cycles"][0]
    assert denied.calls == []
    assert cycle["state"] == "partial"
    assert cycle["gaps"]
    assert any(row["status"] in {"missing", "unverifiable"} for row in cycle["gaps"])
    bundle = verify_bundle(archive, cycle["bundle"]["bundle_id"])
    assert bundle["documents"] == []
    assert bundle["claims"] == []
    queried = execute("evidence", {"bundle_id": cycle["bundle"]["bundle_id"]}, archive_root=archive,
                      service_factory=factory)
    assert queried["data"]["manifest_sha256"] == cycle["bundle"]["manifest_sha256"]
    assert denied.calls == []
