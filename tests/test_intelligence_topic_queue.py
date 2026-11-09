"""Actual detached fixture processes verify the bounded research worker queue.

These jobs perform only labeled local TEST_FIXTURE work. They do not stand in
for the separate real-model checkpoint, schema, receipt or financial tests.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from trading_system.intelligence.contracts import IntelligenceError
from trading_system.intelligence.catalog import SourceCatalog
from trading_system.intelligence.topic import TopicStore, trusted_policy_value, DEFAULT_POLICY
from trading_system.intelligence import topic_cli


FIXTURE_WORKER = r'''
import json, os, sys, time
from pathlib import Path
from trading_system.intelligence import topic_cli
from trading_system.intelligence.topic import TopicStore
from trading_system.agent_evidence import atomic_json
class LocalFixture:
    def __init__(self, root, archive, *, tenant, **_):
        self.root=Path(root)
        self.store=TopicStore(root,tenant=tenant)
    def run(self, job_id):
        job=self.store.job(job_id)
        directory=self.store.directory(job_id)
        with (directory/'worker.lock').open('a+') as lock:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX)
            self.store.update(job_id,state='running',phase='TEST_FIXTURE')
            with (self.root/'fixture-starts.jsonl').open('a') as events:
                events.write(json.dumps({'job_id':job_id,'pid':os.getpid(),'sid':os.getsid(0),'kind':'TEST_FIXTURE'})+chr(10))
                events.flush()
                os.fsync(events.fileno())
            time.sleep(job['request'].get('fixture_delay',0.08))
            self.store.update(job_id,state='completed',phase='TEST_FIXTURE_FINISHED')
topic_cli.TopicResearch=LocalFixture
raise SystemExit(topic_cli.main(sys.argv[1:]))
'''


def setup(tmp_path, monkeypatch, *, tenant="public"):
    root = tmp_path.resolve() / "state"
    store = TopicStore(root, tenant=tenant)
    service = SimpleNamespace(root=root, archive_root=tmp_path.resolve() / "archive", tenant=tenant, store=store)
    fixture = tmp_path / "fixture-worker.py"
    fixture.write_text("import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n" + FIXTURE_WORKER)
    children = []
    original = subprocess.Popen
    def actual_fixture_process(args, **options):
        assert args[:4] == [sys.executable, "-B", "-m", "trading_system.intelligence.topic_cli"]
        assert options["start_new_session"] and options["close_fds"]
        assert len(options["pass_fds"]) == 1 and "--slot-fd" in args
        with (root / ("fixture-child-" + str(len(children)) + ".log")).open("wb") as diagnostics:
            options["stderr"] = diagnostics
            child = original([sys.executable, str(fixture), *args[4:]], **options)
        children.append(child)
        return child
    monkeypatch.setattr(topic_cli.subprocess, "Popen", actual_fixture_process)
    return service, children


def job(service, key, *, delay=0.08, wall_seconds=2400):
    policy = trusted_policy_value({**DEFAULT_POLICY, "wall_seconds": wall_seconds,
        "review_reserve_seconds": 1, "closeout_seconds": 1})
    catalog = SourceCatalog()
    return service.store.create({"idempotency_key": key, "fixture_delay": delay},
        {"schema": "TEST_FIXTURE", "topic_id": "fixture", "version": 1,
            "catalog_sha256": catalog.manifest()["sha256"]}, policy, catalog=catalog)


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Fixture queue did not reach the expected state")


def starts(root):
    path = root / "fixture-starts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def stop(children):
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_burst_has_one_actual_detached_worker_and_fifo_drain(tmp_path, monkeypatch):
    service, children = setup(tmp_path, monkeypatch)
    jobs = [job(service, "burst-" + str(index)) for index in range(20)]
    try:
        for entry in jobs:
            topic_cli.spawn_worker(service, entry)
        assert len(children) == 1
        children[0].wait(timeout=12)
        assert children[0].returncode == 0
        events = starts(service.root)
        assert [event["job_id"] for event in events] == [entry["job_id"] for entry in jobs]
        assert {event["pid"] for event in events} == {children[0].pid}
        assert all(event["sid"] == event["pid"] for event in events)
        assert all(service.store.job(entry["job_id"])["state"] == "completed" for entry in jobs)
        # A terminal original identity starts no replacement process.
        topic_cli.spawn_worker(service, service.store.job(jobs[0]["job_id"]))
        assert len(children) == 1
    finally:
        stop(children)


def test_waiting_job_expires_on_original_admission_clock(tmp_path, monkeypatch):
    service, children = setup(tmp_path, monkeypatch)
    first = job(service, "long-first", delay=3.2, wall_seconds=10)
    expired = job(service, "short-queued", wall_seconds=3)
    try:
        topic_cli.spawn_worker(service, first)
        topic_cli.spawn_worker(service, expired)
        children[0].wait(timeout=8)
        assert children[0].returncode == 0 and len(children) == 1
        assert [event["job_id"] for event in starts(service.root)] == [first["job_id"]]
        result = service.store.job(expired["job_id"])
        assert result["state"] == "failed" and result["error"]["code"] == "WALL_DEADLINE"
        assert result["deadline"] == expired["deadline"]
    finally:
        stop(children)


def test_killed_worker_resumes_same_identity_and_drains_existing_queue(tmp_path, monkeypatch):
    service, children = setup(tmp_path, monkeypatch)
    first = job(service, "interrupted-first", delay=0.4)
    second = job(service, "already-queued")
    try:
        topic_cli.spawn_worker(service, first)
        topic_cli.spawn_worker(service, second)
        wait_until(lambda: len(starts(service.root)) == 1)
        os.killpg(children[0].pid, signal.SIGKILL)
        children[0].wait(timeout=3)
        original = service.store.job(first["job_id"])
        topic_cli.spawn_worker(service, original)
        assert len(children) == 2
        children[1].wait(timeout=8)
        assert children[1].returncode == 0
        assert service.store.job(first["job_id"])["state"] == "completed"
        assert service.store.job(second["job_id"])["state"] == "completed"
        assert service.store.job(first["job_id"])["deadline"] == first["deadline"]
        assert {event["job_id"] for event in starts(service.root)} == {first["job_id"], second["job_id"]}
    finally:
        stop(children)


def test_new_request_during_worker_exit_cannot_lose_its_wakeup(tmp_path, monkeypatch):
    service, children = setup(tmp_path, monkeypatch)
    try:
        for index in range(8):
            entry = job(service, "exit-race-" + str(index), delay=0.025)
            topic_cli.spawn_worker(service, entry)
            wait_until(lambda: service.store.job(entry["job_id"])["state"] == "completed")
        wait_until(lambda: all(child.poll() is not None for child in children))
        assert all(child.returncode == 0 for child in children)
        assert len(starts(service.root)) == 8
    finally:
        stop(children)


def test_other_tenant_requires_a_separate_research_deployment(tmp_path, monkeypatch):
    service, children = setup(tmp_path, monkeypatch)
    topic_cli.admit_research_deployment(service)
    other = SimpleNamespace(root=service.root, tenant="different")
    with pytest.raises(IntelligenceError) as conflict:
        topic_cli.admit_research_deployment(other)
    assert conflict.value.code == "TENANT_ROOT_CONFLICT" and children == []


def test_invalid_inherited_slot_cannot_start_fixture_work(tmp_path, monkeypatch):
    service, children = setup(tmp_path, monkeypatch)
    entry = job(service, "bad-slot")
    fixture = tmp_path / "fixture-worker.py"
    # The ordinary public JSON contract cannot provide this private descriptor.
    # setup patched the module object; use a separate actual Python command
    # through exec_command-style subprocess.run captured before the patch.
    monkeypatch.undo()
    completed = subprocess.run([sys.executable, str(fixture), "_worker", "--state-root", str(service.root),
        "--archive-root", str(service.archive_root), "--mode", "research", "--tenant", "public",
        "--job-id", entry["job_id"], "--slot-fd", "999999"],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert completed.returncode != 0 and starts(service.root) == []
