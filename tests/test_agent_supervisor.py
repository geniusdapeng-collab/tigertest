"""Diagnostic limits and real process-group ownership/reaping assertions."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agent_workflow_fixtures import REQUEST, fixture_source, wait_terminal
import trading_system
from trading_system.agent_evidence import EvidenceInvalid, read_json
from trading_system.agent_runner import submit
from trading_system.agent_runtime import pointer_read
from trading_system.agent_supervisor import (
    BoundedLog, SupervisorInterrupted, _darwin_zombie_group, _kill_group,
    _safe_log_line, _wait_for_owned_exit, error_record,
)
from trading_system import platform_process
from trading_system.redline.execution import StrictObservationError


@pytest.mark.parametrize("line", [
    b"Authorization: Bearer CANARY_SECRET",
    b'{"api_key":"CANARY_SECRET"}',
    b"{'password': 'CANARY_SECRET'}",
    b"api-key=CANARY_SECRET",
    b"https://user:" + b"CANARY_SECRET" + b"@example.test/a",
    b"https://example.test/a?token=CANARY_SECRET",
])
def test_diagnostics_redact_complete_credential_values(line):
    assert b"CANARY_SECRET" not in _safe_log_line(line)


def test_log_budget_and_oversized_lines_have_explicit_truncation_flags(tmp_path):
    # Tiny empty lines expand with labels, so raw input can be much smaller
    # than retained formatted output even though the budget was exhausted.
    log = BoundedLog(tmp_path, limit=64)
    log.consume("stdout", b"\n" * 20)
    summary = log.finish()
    assert summary["received_bytes"] < summary["retained_bytes"]
    assert summary["truncated"] and summary["budget_truncated"]
    assert (tmp_path / "engine.log").stat().st_size == 64
    another = tmp_path / "oversize"
    another.mkdir()
    log = BoundedLog(another)
    log.consume("stderr", b"a" * 20000)
    log.consume("stderr", b"CANARY_SECRET\nnormal\n")
    summary = log.finish()
    assert summary["oversized_lines"] == 1 and summary["truncated"]
    assert b"CANARY_SECRET" not in (another / "engine.log").read_bytes()
    assert b"normal" in (another / "engine.log").read_bytes()


def test_redaction_alone_does_not_claim_budget_truncation(tmp_path):
    log = BoundedLog(tmp_path)
    log.consume("stderr", b"Authorization: Bearer " + b"x" * 3000 + b"\n")
    summary = log.finish()
    assert summary["received_bytes"] > summary["retained_bytes"]
    assert not summary["truncated"] and not summary["budget_truncated"]


@pytest.mark.parametrize("exception,code", [
    (TimeoutError(), "TIMEOUT"), (InterruptedError(), "INTERRUPTED"),
    (SupervisorInterrupted(), "INTERRUPTED"), (PermissionError(), "ARTIFACT_WRITE_FAILED"),
])
def test_terminal_error_code_precedes_oserror_parent_class(exception, code):
    assert error_record(exception, run_id="run-" + "9" * 32)["code"] == code


def test_observation_boundary_has_controlled_data_error_without_arbitrary_codes():
    run_id = "run-" + "8" * 32
    value = error_record(StrictObservationError("CANARY_PRIVATE_DETAIL"),
                         run_id=run_id, stage="data.prepare")
    assert value["code"] == "DATA_UNAVAILABLE"
    assert value["stage"] == "data.prepare"
    assert value["run_id"] == run_id
    assert "CANARY_PRIVATE_DETAIL" not in value["message"]

    class ArbitraryProviderError(RuntimeError):
        code = "DATA_UNAVAILABLE"

    assert error_record(ArbitraryProviderError(), run_id=run_id)["code"] == "RUNNER_FAILED"


def test_group_cleanup_precedes_reap_and_never_targets_own_group():
    with pytest.raises(RuntimeError):
        _kill_group(os.getpgrp())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    try:
        _kill_group(child.pid)
        assert child.returncode is None  # Cleanup did not poll/reap the retained leader.
        assert child.wait(timeout=3) < 0
    finally:
        if child.returncode is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin zombie-group EPERM semantics")
def test_darwin_zombie_only_permission_error_is_proven_before_reap():
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not _darwin_zombie_group(child.pid):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(PermissionError):
            os.killpg(child.pid, signal.SIGTERM)
        _kill_group(child.pid)
        assert child.returncode is None
        assert child.wait(timeout=3) == 0
    finally:
        if child.returncode is None:
            child.wait(timeout=3)


def test_live_permission_failure_is_never_ignored(monkeypatch):
    import trading_system.agent_supervisor as supervisor
    monkeypatch.setattr(supervisor.os, "killpg", lambda *_: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(supervisor, "_darwin_zombie_group", lambda _pgid: False)
    with pytest.raises(PermissionError):
        _kill_group(os.getpid() + 100000)


def test_owned_exit_observation_keeps_leader_waitable_until_group_cleanup():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"],
                             start_new_session=True)
    try:
        _wait_for_owned_exit(child.pid, time.monotonic() + 5)
        assert child.returncode is None
        # The original child remains waitable even after a second observation.
        _wait_for_owned_exit(child.pid, time.monotonic() + 5)
        _kill_group(child.pid)
        assert child.returncode is None
        assert child.wait(timeout=3) == 0
    finally:
        if child.returncode is None:
            _kill_group(child.pid)
            child.wait(timeout=3)


@pytest.mark.parametrize("state", [
    None,
    {"pid": 900001, "ppid": 1, "pgid": 900001, "zombie": True},
    {"pid": 900002, "ppid": "{parent}", "pgid": 900001, "zombie": True},
    {"pid": 900001, "ppid": "{parent}", "pgid": 900002, "zombie": True},
    {"pid": 900001, "ppid": "{parent}", "pgid": 900001, "zombie": False},
])
def test_darwin_owned_exit_refuses_missing_or_conflicting_identity(monkeypatch, state):
    import trading_system.agent_supervisor as supervisor
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    value = state
    if isinstance(value, dict):
        ppid = value["ppid"]
        value = {**value, "ppid": ppid.format(parent=os.getpid())
                 if isinstance(ppid, str) else ppid}
    else:
        monkeypatch.setattr(
            platform_process, "process_group_observation",
            lambda _pgid: {"leader_present": False, "all_zombies": False},
        )
    monkeypatch.setattr(platform_process, "process_identity", lambda _pid: value)
    with pytest.raises(EvidenceInvalid, match="identity"):
        _wait_for_owned_exit(900001, time.monotonic() + 5)


@pytest.mark.parametrize("failure", ["failed", "missing", "slow"])
def test_owned_exit_query_failure_never_becomes_success(monkeypatch, failure):
    import trading_system.agent_supervisor as supervisor
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")

    def broken(*args, **kwargs):
        raise platform_process.ProcessQueryError("private fixture")

    monkeypatch.setattr(platform_process, "process_identity", broken)
    with pytest.raises(EvidenceInvalid, match="query"):
        _wait_for_owned_exit(900001, time.monotonic() + 5)


def test_owned_exit_deadline_wins_over_late_zombie_observation(monkeypatch):
    import trading_system.agent_supervisor as supervisor
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    clock = iter([10.0, 11.0])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))

    def late(*args, **kwargs):
        return {"pid": 900001, "ppid": os.getpid(), "pgid": 900001, "zombie": True}

    monkeypatch.setattr(platform_process, "process_identity", late)
    with pytest.raises(TimeoutError):
        _wait_for_owned_exit(900001, 10.5)


def test_owned_exit_preserves_business_alarm_during_process_query(monkeypatch):
    import trading_system.agent_supervisor as supervisor
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")

    def alarm(*_args, **_kwargs):
        raise TimeoutError("Business deadline elapsed")

    monkeypatch.setattr(platform_process, "process_identity", alarm)
    with pytest.raises(TimeoutError, match="Business deadline elapsed"):
        _wait_for_owned_exit(900001, time.monotonic() + 5)


def test_platform_process_group_requires_retained_leader_and_all_zombies(monkeypatch):
    monkeypatch.setattr(platform_process, "group_pids", lambda _pgid: [41, 42, 43])
    records = {
        41: {"pid": 41, "ppid": 1, "pgid": 41, "zombie": True},
        42: {"pid": 42, "ppid": 41, "pgid": 41, "zombie": True},
    }
    monkeypatch.setattr(platform_process, "process_identity", lambda pid: records.get(pid))
    expected = platform_process.process_group_observation(41)
    assert expected["leader_present"] is True and expected["all_zombies"] is True
    records[43] = {"pid": 43, "ppid": 41, "pgid": 41, "zombie": False}
    live = platform_process.process_group_observation(41)
    assert live["all_zombies"] is False
    monkeypatch.setattr(platform_process, "group_pids", lambda _pgid: [42, 43])
    missing = platform_process.process_group_observation(41)
    assert missing["leader_present"] is False and missing["all_zombies"] is False
    records[42]["pgid"] = 99
    with pytest.raises(platform_process.ProcessQueryError, match="another process group"):
        platform_process.process_group_observation(41)


def test_supervisor_source_never_depends_on_external_ps():
    source = Path(trading_system.__file__).parent / "agent_supervisor.py"
    text = source.read_text(encoding="utf-8")
    assert '["/bin/ps"' not in text
    assert "platform_process.process_identity" in text
    assert "platform_process.process_group_observation" in text


@pytest.mark.parametrize("mode", ["fds", "descendant", "flood"])
def test_engine_fd_isolation_descendant_cleanup_and_large_log_draining(tmp_path, mode):
    source = fixture_source(tmp_path / "source", mode=mode)
    state = tmp_path / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run = state / "runs" / accepted["run_id"]
    observed = wait_terminal(state, run.name, seconds=25)
    assert observed["status"] == "completed_degraded", observed
    if mode == "fds":
        assert read_json(run, "work/descriptor-observation.json")["inherited_lock_fds"] == []
    elif mode == "descendant":
        descendant = read_json(run, "work/descendant-observation.json")
        state_text = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(descendant["pid"])],
            capture_output=True, text=True, check=False, timeout=3).stdout.strip()
        assert not state_text or state_text.startswith("Z"), state_text
        process = read_json(run, "process-exit.json")
        assert process["guardian_pid"] == descendant["pgid"]
    else:
        summary = read_json(run, "log-summary.json")
        assert summary["received_bytes"] > 8 * 1024 * 1024
        assert summary["budget_truncated"] and summary["truncated"]
        assert (run / "engine.log").stat().st_size == 8 * 1024 * 1024


def test_guardian_startup_failure_releases_locks_and_never_publishes(tmp_path):
    source = fixture_source(tmp_path / "source")
    path = source / "trading_system/agent_supervisor.py"
    code = path.read_text()
    marker = '    if os.getpgrp() != os.getpid():\n'
    assert code.count(marker) == 1
    code = code.replace(marker, '    raise RuntimeError("TEST_FIXTURE guardian startup failed")\n' + marker)
    path.write_text(code)
    state = tmp_path / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    observed = wait_terminal(state, accepted["run_id"])
    assert observed["status"] == "failed"
    assert pointer_read(state) is None
    from trading_system.agent_runner import _lock_fd
    fd = _lock_fd(state, "account.lock", create=False, nonblocking=True)
    os.close(fd)


@pytest.mark.parametrize("after_eof", ["normal", "nonzero", "never"])
def test_guardian_control_eof_waits_for_real_exit_before_group_cleanup(tmp_path, after_eof):
    """A closed pipe does not certify that its still-owned writer has exited."""
    source = fixture_source(tmp_path / "source")
    path = source / "trading_system/agent_supervisor.py"
    code = path.read_text()
    marker = "        os.close(control_fd)\n\n\ndef _read_control_until_eof"
    assert code.count(marker) == 1
    delay = 60 if after_eof == "never" else 0.75
    ending = "        os._exit(7)\n" if after_eof == "nonzero" else ""
    code = code.replace(marker,
        '        os.close(control_fd)\n'
        '        atomic_json(run, "guardian-after-eof.json", {"fixture": "TEST_FIXTURE", "entered": True})\n'
        f'        time.sleep({delay})\n'
        '        atomic_json(run, "guardian-after-eof-done.json", {"fixture": "TEST_FIXTURE", "finished": True})\n'
        + ending + "\n\ndef _read_control_until_eof")
    if after_eof == "never":
        # A private fixture cutoff covers this precise post-EOF wait. It does
        # not change the public 2400-second deadline or claim it elapsed.
        cutoff = "            rows = _read_control_until_eof(control_read, deadline)\n"
        assert code.count(cutoff) == 1
        code = code.replace(cutoff, cutoff +
                            "            deadline = min(deadline, time.monotonic() + 1.5)\n")
    path.write_text(code)
    state = tmp_path / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run = state / "runs" / accepted["run_id"]
    observed = wait_terminal(state, run.name, seconds=20)
    assert read_json(run, "engine-result.json")["success"] is True
    assert read_json(run, "guardian-after-eof.json")["entered"] is True
    if after_eof == "normal":
        assert observed["status"] == "completed_degraded", observed
        assert read_json(run, "guardian-after-eof-done.json")["finished"] is True
        process = read_json(run, "process-exit.json")
        assert process["guardian_exit"] == process["engine_exit"] == 0
        assert process["group_cleanup_before_reap"] is True
        assert pointer_read(state) is not None
    else:
        assert observed["status"] == "failed", observed
        assert observed["error"]["code"] == ("TIMEOUT" if after_eof == "never" else "RUNNER_FAILED")
        assert pointer_read(state) is None
        assert not (run / "process-exit.json").exists()
        if after_eof == "nonzero":
            assert read_json(run, "guardian-after-eof-done.json")["finished"] is True
    from trading_system.agent_runner import _lock_fd
    fd = _lock_fd(state, "account.lock", create=False, nonblocking=True)
    os.close(fd)


@pytest.mark.parametrize("mutation", [
    'rows[0].pop("pid")', 'rows[0].update(pid=True)', 'rows[0].update(pid=0)',
    'rows[0].update(extra="TEST_FIXTURE")', 'rows[1].update(exit_code=False)',
    'rows.reverse()', 'rows[0] = None',
])
def test_malformed_control_fails_before_entering_exit_wait(tmp_path, mutation):
    source = fixture_source(tmp_path / "source")
    path = source / "trading_system/agent_supervisor.py"
    code = path.read_text()
    read_marker = "            rows = _read_control_until_eof(control_read, deadline)\n"
    wait_marker = "            _wait_for_owned_exit(leader.pid, deadline)\n"
    assert code.count(read_marker) == code.count(wait_marker) == 1
    code = code.replace(read_marker, read_marker + f"            {mutation}\n")
    code = code.replace(wait_marker,
        '            atomic_json(run, "invalid-control-wait-entered.json", {"fixture": "TEST_FIXTURE"})\n'
        + wait_marker)
    path.write_text(code)
    state = tmp_path / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run = state / "runs" / accepted["run_id"]
    observed = wait_terminal(state, run.name)
    assert read_json(run, "engine-result.json")["success"] is True
    assert observed["status"] == "failed" and observed["error"]["code"] == "RUNNER_FAILED"
    assert not (run / "invalid-control-wait-entered.json").exists()
    assert not (run / "process-exit.json").exists() and pointer_read(state) is None
