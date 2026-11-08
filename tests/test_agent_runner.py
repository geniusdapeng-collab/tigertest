"""Public request/permission/lifecycle boundaries through real local processes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agent_workflow_fixtures import REQUEST, fixture_source, wait_engine, wait_terminal
from trading_system import agent_runner as runner
from trading_system.agent_evidence import atomic_json, read_json
from trading_system.agent_runtime import SOURCE_ROOT, frozen_clock, pointer_read, verify_run


def invoke(state, operation, request, *, mode="readonly"):
    payload = request if isinstance(request, bytes) else json.dumps(request).encode()
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "trading_system.agent_runner",
         "--mode", mode, "--state-root", str(state), operation],
        input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=SOURCE_ROOT, timeout=15,
    )
    return completed, json.loads(completed.stdout)


@pytest.mark.parametrize("operation,body", [
    ("doctor", {}), ("describe", {"profile": "us-public-paper-v1"}),
    ("run", REQUEST), ("status", {"run_id": "run-" + "2" * 32}),
])
def test_readonly_operations_never_create_state_or_launch_market(tmp_path, operation, body):
    state = tmp_path / "state"
    process, value = invoke(state, operation, body)
    assert not state.exists()
    assert value["operation"] == operation
    assert value["summary"] is None and value["verification"] is None
    assert process.stderr == b""
    if operation == "run":
        assert process.returncode == 3
        assert value["error"]["code"] == "READ_ONLY"
    elif operation == "status":
        assert process.returncode == 7
        assert value["status"] == "not_found"
    else:
        assert process.returncode == 0 and value["operation_ok"]


@pytest.mark.parametrize("version,supported", [
    ((3, 10, 16), False), ((3, 11, 0), True), ((3, 12, 0), True),
])
def test_python_minimum_is_enforced_before_business_or_state_writes(
        tmp_path, monkeypatch, version, supported):
    monkeypatch.setattr(runner.sys, "version_info", version)
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda _name: "installed")
    state = tmp_path / "state"
    doctor = runner.doctor(state, "readonly")
    assert doctor["operation_ok"] is supported
    assert doctor["status"] == ("ready" if supported else "not_ready")
    if not supported:
        assert doctor["error"]["code"] == "RUNNER_FAILED"
        assert "3.11" in doctor["error"]["message"]
        value = runner.execute("run", REQUEST, mode="paper", state=state)
        assert not value["operation_ok"] and value["error"]["code"] == "RUNNER_FAILED"
        assert value["run_id"] is None
    assert not state.exists()


@pytest.mark.parametrize("payload", [
    b'{"profile":"us-public-paper-v1","profile":"us-public-paper-v1"}',
    b'{"profile":NaN}', b'{"profile":1e999}', b'{}\n{}',
    '{"profile":"us-public-paper-v1"}'.encode("utf-16"),
    b"x" * 65537,
])
def test_raw_protocol_rejects_ambiguity_before_side_effects(tmp_path, payload):
    state = tmp_path / "state"
    process, value = invoke(state, "describe", payload)
    assert process.returncode == 2 and not value["operation_ok"]
    assert value["error"]["code"] in {"INVALID_REQUEST", "INPUT_TOO_LARGE"}
    assert not state.exists()


@pytest.mark.parametrize("extra", [
    {"skip": ["review.daily"]}, {"argv": ["sh"]}, {"env": {"KEY": "secret"}},
    {"path": "/tmp/escape"}, {"provider": "demo"}, {"live": True},
    {"paper_account": "broker"}, {"idempotency_key": "../no"},
])
def test_public_request_cannot_override_execution(tmp_path, extra):
    state = tmp_path / "state"
    value = runner.execute("run", REQUEST | extra, mode="paper", state=state)
    assert not value["operation_ok"] and value["error"]["code"] == "INVALID_REQUEST"
    assert not state.exists()


def test_fixed_worker_success_receipt_and_same_key_recovery(tmp_path):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    assert accepted["status"] == "accepted" and accepted["summary"] is None
    run_id = accepted["run_id"]
    observed = wait_terminal(state, run_id)
    assert observed["status"] == "completed_degraded", observed
    verified = verify_run(state, run_id, current_source=source)
    assert verified["verification"]["ok"]
    assert verified["summary"]["cash_usd"] == 100000
    assert verified["summary"]["market_view"] == "TEST_FIXTURE"
    before = pointer_read(state)
    replay = runner.submit(state, REQUEST, source=source)
    assert replay["run_id"] == run_id and replay["status"] == "accepted"
    assert replay["data"] == {"idempotent_replay": True, "observed_status": "completed_degraded"}
    assert replay["summary"] is None and replay["verification"] is None
    assert pointer_read(state) == before
    assert len(list((state / "runs").iterdir())) == 1
    receipt = runner.execute("receipt", {"run_id": run_id}, mode="readonly", state=state)
    verify = runner.execute("verify", {"run_id": run_id}, mode="readonly", state=state)
    assert receipt["summary"] and receipt["verification"]["ok"]
    assert verify["summary"] is None and verify["verification"]["ok"]


@pytest.mark.parametrize("overlay", [None, b'{"macro":{"2026-10-20":"TEST_EVENT"},"earnings":{}}\n'])
def test_calendar_absence_is_explicit_and_present_overlay_is_preserved(tmp_path, overlay):
    source = fixture_source(tmp_path / "source")
    if overlay is not None:
        (source / "events_calendar.json").write_bytes(overlay)
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run = state / "runs" / accepted["run_id"]
    assert wait_terminal(state, run.name)["status"] == "completed_degraded"
    expected = overlay if overlay is not None else b'{"earnings":{},"macro":{}}\n'
    assert (run / "work/events_calendar.json").read_bytes() == expected
    calendar = read_json(run, "calendar-input.json")
    assert calendar["source"] == ("frozen_overlay" if overlay is not None else "builtin_only")
    assert calendar["sha256"] == runner.digest(expected)
    from trading_system.events import EventCalendar
    actual = EventCalendar(str(run / "work/events_calendar.json"), strict=True)
    assert "FOMC" in actual.macro_events("2026-10-28")
    if overlay is not None:
        assert actual.macro_events("2026-10-20") == ["TEST_EVENT"]
    assert verify_run(state, run.name)["verification"]["ok"]
    (run / "work/events_calendar.json").write_bytes(b'{"macro":{},"earnings":{"AAPL":"2026-10-21"}}\n')
    with pytest.raises(runner.EvidenceInvalid, match="Calendar"):
        verify_run(state, run.name)


def test_same_key_concurrency_and_account_busy_do_not_duplicate_worker(tmp_path):
    source = fixture_source(tmp_path / "source", mode="hang")
    state = tmp_path / "state"
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: runner.submit(state, REQUEST, source=source), range(2)))
    run_ids = {value["run_id"] for value in values}
    assert len(run_ids) == 1 and all(value["operation_ok"] for value in values)
    run_id = values[0]["run_id"]
    processes = wait_engine(state, run_id)
    try:
        another = runner.submit(state, REQUEST | {"idempotency_key": "fixture-key-0002"}, source=source)
        assert another["error"]["code"] == "ACCOUNT_BUSY"
        assert another["run_id"] is None
        assert len(list((state / "keys").iterdir())) == 1
        assert len(list((state / "runs").iterdir())) == 1
    finally:
        os.kill(processes["supervisor_pid"], signal.SIGTERM)
        terminal = wait_terminal(state, run_id)
    assert terminal["status"] == "interrupted"
    assert terminal["error"]["code"] == "INTERRUPTED"
    assert pointer_read(state) is None


def test_failure_is_not_a_successful_replay_and_status_query_is_successful(tmp_path):
    source = fixture_source(tmp_path / "source", mode="fail")
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    run_id = accepted["run_id"]
    terminal = wait_terminal(state, run_id)
    assert terminal["status"] == "failed", terminal
    replay = runner.submit(state, REQUEST, source=source)
    assert not replay["operation_ok"] and replay["status"] == "failed"
    assert replay["run_id"] == run_id and pointer_read(state) is None
    query = runner.execute("status", {"run_id": run_id}, mode="readonly", state=state)
    assert query["operation_ok"] and query["status"] == "failed"
    assert query["error"] is None and query["data"]["error"]["code"] == "RUNNER_FAILED"
    receipt = runner.execute("receipt", {"run_id": run_id}, mode="readonly", state=state)
    assert not receipt["operation_ok"] and receipt["summary"] is None


def test_task_deadline_terminal_failure_is_queryable(tmp_path):
    state = tmp_path / "state"
    run_id = "run-" + "a" * 32
    run = state / "runs" / run_id
    run.mkdir(parents=True)
    (run / "run.lock").touch()
    error = {
        "code": "TASK_DEADLINE",
        "message": "Work-unit deadline has expired",
        "run_id": run_id,
        "stage": "clean.llm_semantic",
        "retryable": False,
        "details_ref": "engine-result.json",
    }
    atomic_json(run, "status.json", {
        "schema": "tiger.status.v1",
        "run_id": run_id,
        "status": "failed",
        "error": error,
    })
    observed = runner.execute("status", {"run_id": run_id}, mode="readonly", state=state)
    assert observed["operation_ok"] is True
    assert observed["status"] == "failed"
    assert observed["data"]["error"] == error


def test_lost_acceptance_reply_recovers_existing_identity_without_new_worker(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    def lost(*_args, **_kwargs):
        raise runner.EvidenceInvalid("TIMEOUT", "TEST_FIXTURE lost acknowledgement")
    monkeypatch.setattr(runner, "_acknowledgement", lost)
    unknown_delivery = runner.submit(state, REQUEST, source=source)
    assert unknown_delivery["error"]["code"] == "TIMEOUT"
    run_id = unknown_delivery["run_id"]
    terminal = wait_terminal(state, run_id)
    assert terminal["status"] == "completed_degraded", terminal
    replay = runner.submit(state, REQUEST, source=source)
    assert replay["run_id"] == run_id and replay["operation_ok"]
    assert len(list((state / "runs").iterdir())) == 1


def test_mutable_completed_status_is_not_a_commit(tmp_path):
    source = fixture_source(tmp_path / "source", mode="fail")
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    run_id = accepted["run_id"]
    wait_terminal(state, run_id)
    atomic_json(state / "runs" / run_id, "status.json", {
        "schema": "tiger.status.v1", "run_id": run_id,
        "status": "completed", "error": None,
    })
    query = runner.execute("status", {"run_id": run_id}, mode="readonly", state=state)
    assert query["status"] == "interrupted" and query["summary"] is None


def test_guardian_kills_stopped_engine_after_supervisor_sigkill(tmp_path):
    source = fixture_source(tmp_path / "source", mode="stop")
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    run_id = accepted["run_id"]
    processes = wait_engine(state, run_id)
    stop_deadline = time.monotonic() + 10
    while True:
        observed_state = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(processes["engine_pid"])],
            capture_output=True, text=True, timeout=3).stdout.strip()
        if "T" in observed_state:
            break
        assert time.monotonic() < stop_deadline, observed_state
        time.sleep(0.02)
    os.kill(processes["supervisor_pid"], signal.SIGKILL)
    terminal = wait_terminal(state, run_id)
    assert terminal["status"] == "interrupted"
    assert pointer_read(state) is None
    # No reliance on PID existence: a zombie may await the host's init reaper,
    # but cannot execute or retain advisory lock descriptors.
    fd = runner._lock_fd(state, "account.lock", create=False, nonblocking=True)
    os.close(fd)
    output = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(processes["engine_pid"])],
                            capture_output=True, text=True, timeout=3).stdout.strip()
    assert not output or output.startswith("Z"), output


def test_deadline_failure_has_timeout_code_and_no_publication(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "source", mode="hang")
    state = tmp_path / "state"
    monkeypatch.setattr(runner, "frozen_clock", lambda: frozen_clock(
        datetime.now(timezone.utc) - timedelta(seconds=2398)))
    accepted = runner.submit(state, REQUEST, source=source)
    run_id = accepted["run_id"]
    terminal = wait_terminal(state, run_id, seconds=10)
    assert terminal["status"] == "failed"
    assert terminal["error"]["code"] == "TIMEOUT", terminal
    assert pointer_read(state) is None


@pytest.mark.parametrize("mode,terminal_status", [
    ("success", "completed_degraded"), ("fail", "failed"),
])
def test_status_does_not_mix_precompletion_evidence_with_postcompletion_liveness(
        tmp_path, monkeypatch, mode, terminal_status):
    source = fixture_source(tmp_path / "source", mode=mode)
    entry = source / "main.py"
    original = entry.read_text()
    anchor = "def _daily(args, provider, *, execution):\n"
    assert original.count(anchor) == 1
    entry.write_text(original.replace(anchor, anchor + (
        '    while not (execution.paths.work_dir / "query-release").exists():\n'
        "        time.sleep(0.01)\n"
    )))
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run_id = accepted["run_id"]
    run = state / "runs" / run_id
    release = run / "work" / "query-release"
    original_pointer = runner.committed_pointer
    crossed = []

    def finish_after_pointer_snapshot(actual_state, actual_run_id):
        snapshot = original_pointer(actual_state, actual_run_id)
        if actual_run_id == run_id and not crossed:
            assert snapshot is None and runner._is_active(run)
            assert read_json(run, "status.json")["status"] == "running"
            crossed.append("before-completion")
            release.touch()
            deadline = time.monotonic() + 20
            while True:
                persisted = read_json(run, "status.json")
                if persisted["status"] == terminal_status and not runner._is_active(run):
                    crossed.append("after-completion")
                    break
                assert time.monotonic() < deadline, persisted
                time.sleep(0.01)
        return snapshot

    try:
        monkeypatch.setattr(runner, "committed_pointer", finish_after_pointer_snapshot)
        query = runner.execute("status", {"run_id": run_id}, mode="readonly", state=state)
        assert crossed == ["before-completion", "after-completion"]
        assert query["operation_ok"], query
        # A conservative running observation is safe; an invented interrupted
        # terminal state would make a client stop waiting on the actual run.
        assert query["status"] in {"running", terminal_status}, query
        assert query["data"]["error"] is None or query["status"] == terminal_status
        settled = runner.execute("status", {"run_id": run_id}, mode="readonly", state=state)
        assert settled["status"] == terminal_status and not settled["data"]["active"]
        receipt = runner.execute("receipt", {"run_id": run_id}, mode="readonly", state=state)
        if mode == "success":
            assert pointer_read(state)["run_id"] == run_id
            assert receipt["operation_ok"] and receipt["verification"]["ok"]
        else:
            assert pointer_read(state) is None
            assert settled["data"]["error"]["code"] == "RUNNER_FAILED"
            assert not receipt["operation_ok"] and receipt["summary"] is None
    finally:
        release.touch()
        wait_terminal(state, run_id)
