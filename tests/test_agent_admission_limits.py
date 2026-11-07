"""Preparation has its own deadline before detached worker acceptance exists."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import signal
import threading
import time

import pytest

from agent_workflow_fixtures import REQUEST, fixture_source, wait_terminal
from trading_system import agent_runner as runner
from trading_system import agent_supervisor as supervisor
from trading_system.admission_guards import PreparationDeadline
from trading_system.agent_evidence import EvidenceInvalid, checked_root, read_json
from trading_system.agent_runtime import pointer_read


def test_slow_source_preparation_times_out_and_preserves_failed_key(tmp_path, monkeypatch):
    state = tmp_path / "state"
    calls = []
    def slow(*_args):
        calls.append("freeze")
        time.sleep(2)
    monkeypatch.setattr(runner, "freeze_source", slow)
    started = time.monotonic()
    value = runner.submit(state, REQUEST, source=tmp_path, _preparation_seconds=0.08)
    assert time.monotonic() - started < 1
    assert calls == ["freeze"]
    assert not value["operation_ok"] and value["error"]["code"] == "TIMEOUT"
    run_id = value["run_id"]
    assert run_id and pointer_read(state) is None
    stored = read_json(state / "runs" / run_id, "status.json")
    assert stored["status"] == "failed" and stored["error"]["stage"] == "admission.prepare"
    replay = runner.submit(state, REQUEST, source=tmp_path)
    assert replay["run_id"] == run_id and replay["status"] == "failed"
    assert replay["error"]["code"] == "TIMEOUT" and calls == ["freeze"]
    assert not (state / "runs" / run_id / "accepted.json").exists()
    descriptor = runner._lock_fd(state, "account.lock", create=False, nonblocking=True)
    os.close(descriptor)


def test_deadline_is_not_swallowed_by_ordinary_library_error_handler(tmp_path, monkeypatch):
    swallowed = []
    def library(*_args):
        try:
            time.sleep(2)
        except Exception:
            swallowed.append(True)
    monkeypatch.setattr(runner, "freeze_source", library)
    result = runner.submit(tmp_path / "state", REQUEST, source=tmp_path, _preparation_seconds=0.04)
    assert result["error"]["code"] == "TIMEOUT" and not swallowed


def test_slow_archive_staging_is_inside_preparation_deadline(tmp_path, monkeypatch):
    from trading_system import intelligence_workflow
    monkeypatch.setattr(runner, "freeze_source", lambda *_: None)
    reached = []
    def slow(*_args, **_kwargs):
        reached.append("archive")
        time.sleep(2)
    monkeypatch.setattr(intelligence_workflow, "stage_input_packet", slow)
    request = REQUEST | {"profile": "us-public-intelligence-paper-v2"}
    value = runner.submit(tmp_path / "state", request, source=tmp_path, _preparation_seconds=0.1)
    assert reached == ["archive"]
    assert value["error"]["code"] == "TIMEOUT" and value["run_id"]
    assert pointer_read(tmp_path / "state") is None


def test_slow_account_copy_is_inside_preparation_deadline(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "source")
    reached = []
    previous_handler = signal.getsignal(signal.SIGALRM)
    def slow(*_args):
        # Source freezing precedes this operation and varies with filesystem
        # speed. Inject expiry at the operation under test, under the actual
        # original admission handler, rather than requiring all preparation
        # before this point to finish in 500 ms on every hosted runner.
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= 45 and interval == 0
        assert callable(signal.getsignal(signal.SIGALRM))
        assert signal.getsignal(signal.SIGALRM) != previous_handler
        reached.append("account")
        signal.setitimer(signal.ITIMER_REAL, 0.02)
        time.sleep(2)
    monkeypatch.setattr(runner, "stage_account", slow)
    result = runner.submit(tmp_path / "state", REQUEST, source=source)
    assert reached == ["account"]
    assert result["error"]["code"] == "TIMEOUT"
    assert pointer_read(tmp_path / "state") is None
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    replay = runner.submit(tmp_path / "state", REQUEST, source=source)
    assert replay["run_id"] == result["run_id"] and replay["status"] == "failed"
    assert replay["error"]["code"] == "TIMEOUT" and reached == ["account"]


def test_submission_lock_wait_uses_same_bounded_preparation_window(tmp_path):
    state = checked_root(tmp_path / "state", create=True)
    descriptor = runner._lock_fd(state, "submission.lock", create=True, nonblocking=True)
    started = time.monotonic()
    try:
        value = runner.submit(state, REQUEST, source=tmp_path, _preparation_seconds=0.08)
    finally:
        os.close(descriptor)
    assert time.monotonic() - started < 1
    assert value["error"]["code"] == "TIMEOUT" and value["run_id"] is None
    assert not (state / "keys").exists()


def test_guard_preserves_longer_outer_timer_without_extending_it():
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    def outer(*_args):
        raise AssertionError("Outer timer unexpectedly fired")
    try:
        signal.signal(signal.SIGALRM, outer)
        signal.setitimer(signal.ITIMER_REAL, 2)
        with PreparationDeadline(0.2):
            time.sleep(0.02)
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 1 < remaining < 2 and interval == 0
        assert signal.getsignal(signal.SIGALRM) is outer
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_earlier_outer_alarm_keeps_its_handler_and_deadline():
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    called = []
    class OuterExpired(RuntimeError):
        pass
    def outer(*_args):
        called.append(True)
        raise OuterExpired
    try:
        signal.signal(signal.SIGALRM, outer)
        signal.setitimer(signal.ITIMER_REAL, 0.04)
        started = time.monotonic()
        with pytest.raises(OuterExpired):
            with PreparationDeadline(1):
                time.sleep(2)
        assert called == [True] and time.monotonic() - started < 0.5
        assert signal.getsignal(signal.SIGALRM) is outer
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_guard_rejects_direct_nonmain_signal_installation():
    failures = []
    def attempt():
        try:
            with PreparationDeadline(1):
                raise AssertionError("unreachable")
        except EvidenceInvalid as error:
            failures.append(error.code)
    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and failures == ["RUNNER_FAILED"]


def test_supervisor_bootstrap_validation_has_deadline_before_acceptance(tmp_path, monkeypatch):
    state = checked_root(tmp_path / "state", create=True)
    run_id = "run-" + "a" * 32
    run = checked_root(state / "runs" / run_id, create=True)
    account_fd = runner._lock_fd(state, "account.lock", create=True, nonblocking=True)
    run_fd = runner._lock_fd(run, "run.lock", create=True, nonblocking=True)
    ack_read, ack_write = os.pipe()
    def slow(_run):
        time.sleep(2)
        raise AssertionError("unreachable")
    monkeypatch.setattr(supervisor, "validate_admission", slow)
    monkeypatch.setattr(supervisor, "BOOTSTRAP_SECONDS", 0.05)
    previous = signal.getsignal(signal.SIGALRM)
    try:
        started = time.monotonic()
        code = supervisor.supervisor(state, run_id, account_fd=account_fd, run_fd=run_fd, ack_fd=ack_write)
        assert code == 1 and time.monotonic() - started < 1
        assert signal.getsignal(signal.SIGALRM) == previous
        result = read_json(run, "status.json")
        assert result["status"] == "failed" and result["error"]["code"] == "TIMEOUT"
        assert result["error"]["stage"] == "supervisor.bootstrap"
        assert not result["acceptance_was_durable"]
        assert not (run / "accepted.json").exists() and pointer_read(state) is None
        assert os.read(ack_read, 4096) == b""
    finally:
        os.close(ack_read)


@pytest.mark.parametrize("business_seconds", [2400, 5400])
def test_supervisor_business_deadline_uses_original_clock_after_bootstrap(tmp_path, monkeypatch, business_seconds):
    from trading_system.agent_evidence import atomic_json
    state = checked_root(tmp_path / "state", create=True)
    run_id = "run-" + "b" * 32
    run = checked_root(state / "runs" / run_id, create=True)
    account_fd = runner._lock_fd(state, "account.lock", create=True, nonblocking=True)
    run_fd = runner._lock_fd(run, "run.lock", create=True, nonblocking=True)
    ack_read, ack_write = os.pipe()
    original_deadline = datetime.now(timezone.utc) + timedelta(seconds=0.12)
    atomic_json(run, "admission.json", {"fixture": "TEST_FIXTURE"})
    def validate(_run):
        time.sleep(0.04)
        return {"clock": {"deadline_at_utc": original_deadline.isoformat()}}, {"business_deadline_seconds": business_seconds}, {}
    def slow_business(*_args, **kwargs):
        assert 0 < kwargs["deadline"] - time.monotonic() < 0.12
        time.sleep(2)
    monkeypatch.setattr(supervisor, "validate_admission", validate)
    monkeypatch.setattr(supervisor, "supervise_group", slow_business)
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    try:
        started = time.monotonic()
        code = supervisor.supervisor(state, run_id, account_fd=account_fd, run_fd=run_fd, ack_fd=ack_write)
        assert code == 1 and time.monotonic() - started < 0.5
        status = read_json(run, "status.json")
        assert status["error"]["code"] == "TIMEOUT" and status["acceptance_was_durable"]
        assert status["error"]["stage"] == "supervisor.business"
        assert pointer_read(state) is None
    finally:
        os.close(ack_read)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def test_ack_observation_timeout_does_not_kill_accepted_detached_worker(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    original = runner._acknowledgement
    previous_handler = signal.getsignal(signal.SIGALRM)
    acknowledged = []
    def lost_after_acceptance(*args):
        original(*args)
        # Only inject the lost-observation deadline after a genuine durable
        # acknowledgement. This separates the tested parent observation
        # timeout from variable source-copy and worker-bootstrap latency.
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= 45 and interval == 0
        assert callable(signal.getsignal(signal.SIGALRM))
        assert signal.getsignal(signal.SIGALRM) != previous_handler
        acknowledged.append(True)
        signal.setitimer(signal.ITIMER_REAL, 0.02)
        time.sleep(2)
    monkeypatch.setattr(runner, "_acknowledgement", lost_after_acceptance)
    value = runner.submit(state, REQUEST, source=source)
    assert acknowledged == [True]
    assert value["error"]["code"] == "TIMEOUT" and value["run_id"]
    terminal = wait_terminal(state, value["run_id"])
    assert terminal["status"] == "completed_degraded", terminal
    assert pointer_read(state)["run_id"] == value["run_id"]
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_thread_submit_keeps_same_key_concurrency_and_real_worker_semantics(tmp_path):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runner.submit(state, REQUEST, source=source), range(2)))
    assert all(result["operation_ok"] for result in results), results
    assert len({result["run_id"] for result in results}) == 1
    run_id = results[0]["run_id"]
    assert wait_terminal(state, run_id)["status"] == "completed_degraded"
    assert len(list((state / "runs").iterdir())) == 1
