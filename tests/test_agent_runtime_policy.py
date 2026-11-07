"""Trusted startup runtime configuration and historical contract isolation.

Only temporary local files and explicit offline process fixtures are used.
These tests never call a market source, provider, broker or paid model.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from agent_workflow_fixtures import REQUEST, fixture_source, wait_terminal
from trading_system import agent_runner as runner
from trading_system import agent_runtime as runtime
from trading_system import agent_supervisor as supervisor
from trading_system.agent_evidence import (
    EvidenceInvalid, atomic_json, checked_root, digest, json_bytes, read_bytes, read_json,
)

PROFILE = runtime.INTELLIGENCE_PROFILE
V2_REQUEST = REQUEST | {"profile": PROFILE}


@pytest.fixture(autouse=True)
def isolated_startup(monkeypatch):
    for key in (runtime.RUNTIME_POLICY_ENV, "TIGER_INTEL_REGISTRY_FILE", "TIGER_INTEL_ARCHIVE_ROOT", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def configured(tmp_path, monkeypatch, seconds=5400, *, raw=None):
    path = tmp_path / "trusted-runtime.json"
    path.write_bytes(raw if raw is not None else json_bytes({
        "schema": runtime.RUNTIME_POLICY_SCHEMA, "business_deadline_seconds": seconds}))
    monkeypatch.setenv(runtime.RUNTIME_POLICY_ENV, str(path))
    return path


def prepared(tmp_path, monkeypatch, seconds=5400):
    configured(tmp_path, monkeypatch, seconds)
    packet = runtime.startup_runtime_input(PROFILE)
    state = tmp_path / "state"
    run = checked_root(state / "runs" / ("run-" + "c" * 32), create=True)
    source = runtime.freeze_source(run)
    clock = runtime.frozen_clock(seconds=seconds)
    from trading_system.intelligence_workflow import stage_input_packet
    intelligence = stage_input_packet(run, as_of=datetime.fromisoformat(clock["admitted_at_utc"]))
    policy = runtime.policy_manifest(PROFILE, intelligence_input=intelligence, runtime_input=packet,
        scene_policy_path=run / "source" / runtime.SCENE_POLICY_RELATIVE)
    atomic_json(run, "request.json", V2_REQUEST)
    atomic_json(run, "runtime-input.json", packet)
    atomic_json(run, "policy.json", policy)
    runtime.stage_calendar(run)
    admission = {"schema": "tiger.admission.v1", "run_id": run.name, "clock": clock, "parent": None,
        "request_sha256": digest(json_bytes(V2_REQUEST)), "policy_sha256": digest(json_bytes(policy)),
        "source_sha256": digest(read_bytes(run, "source-manifest.json")),
        "runtime_input_sha256": digest(json_bytes(packet))}
    atomic_json(run, "admission.json", admission)
    return state, run, admission, policy, source


@pytest.mark.parametrize("seconds", [2400, 5400, 7200])
def test_valid_trusted_file_freezes_only_nonsecret_bounded_input(tmp_path, monkeypatch, seconds):
    path = configured(tmp_path, monkeypatch, seconds)
    packet = runtime.startup_runtime_input(PROFILE)
    assert packet == {"schema": runtime.RUNTIME_INPUT_SCHEMA, "profile": PROFILE,
                      "business_deadline_seconds": seconds}
    policy = runtime.policy_manifest(PROFILE, runtime_input=packet)
    assert policy["business_deadline_seconds"] == seconds
    assert policy["runtime_input_sha256"] == digest(json_bytes(packet))
    assert str(path).encode() not in json_bytes(packet)
    assert policy["model_contract"]["max_output_tokens"] == 393216
    assert policy["model_contract"]["context_tokens"] == 1048576
    assert len(policy["stages"]) == 41 and policy["real_broker"] is False


@pytest.mark.parametrize("raw", [
    b"", b"[]", b"null", b"{", b"{}", b"\xff",
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":true}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":5400.0}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":"5400"}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":2399}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":7201}',
    b'{"schema":"unknown","business_deadline_seconds":5400}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":5400,"api_key":"CANARY_PRIVATE"}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":5400,"profile":"us-public-paper-v1"}',
    b'{"schema":"tiger.agent-runtime-policy/v1","business_deadline_seconds":5400,"business_deadline_seconds":6000}',
    b" " * 4097,
])
def test_malformed_or_overbroad_runtime_files_fail_closed_without_private_value(tmp_path, monkeypatch, raw):
    configured(tmp_path, monkeypatch, raw=raw)
    with pytest.raises(EvidenceInvalid) as caught:
        runtime.startup_runtime_input(PROFILE)
    assert caught.value.code == "RESULT_INVALID"
    assert "CANARY_PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("kind", ["relative", "missing", "file_symlink", "parent_symlink", "hardlink", "directory"])
def test_unsafe_runtime_path_or_file_identity_is_rejected(tmp_path, monkeypatch, kind):
    path = configured(tmp_path, monkeypatch)
    if kind == "relative":
        location = path.name
    elif kind == "missing":
        location = str(tmp_path / "missing.json")
    elif kind == "file_symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        location = str(link)
    elif kind == "parent_symlink":
        link = tmp_path / "linked-parent"
        link.symlink_to(tmp_path, target_is_directory=True)
        location = str(link / path.name)
    elif kind == "hardlink":
        os.link(path, tmp_path / "second-link.json")
        location = str(path)
    else:
        location = str(tmp_path)
    monkeypatch.setenv(runtime.RUNTIME_POLICY_ENV, location)
    with pytest.raises(EvidenceInvalid):
        runtime.startup_runtime_input(PROFILE)


@pytest.mark.parametrize("profile", sorted(runtime.SUPPORTED_PROFILES - {PROFILE}))
def test_runtime_extension_is_explicitly_restricted_to_v2(tmp_path, monkeypatch, profile):
    configured(tmp_path, monkeypatch)
    with pytest.raises(EvidenceInvalid) as caught:
        runtime.startup_runtime_input(profile)
    assert caught.value.code == "INVALID_REQUEST"
    packet = runtime.startup_runtime_input(PROFILE)
    with pytest.raises(EvidenceInvalid):
        runtime.policy_manifest(profile, runtime_input=packet)


@pytest.mark.parametrize("field", ["runtime_policy", "runtime_policy_file", "runtime_input", "business_deadline_seconds", runtime.RUNTIME_POLICY_ENV])
def test_public_business_request_never_accepts_runtime_configuration(field):
    with pytest.raises(EvidenceInvalid) as caught:
        runner.validate_request("run", V2_REQUEST | {field: "/untrusted/file.json"})
    assert caught.value.code == "INVALID_REQUEST"


def test_default_policy_bytes_and_original_admission_shape_remain_historical(tmp_path, monkeypatch):
    # Captured independently from the pre-extension frozen 1153593 source.
    legacy_hashes = {
        "us-public-active-paper-v1": "477a5c2c4e43560401198c4eb87927366a30ee80228f81f9327e8f9e1ae0d93e",
        PROFILE: "2ad9cf2892f9ba69918c1276aa2448201c4c6ac508fdcbc319c7550650bb631e",
        "us-public-model-required-paper-v1": "6fc4d695468f70dd2157343f3dfbe131043fe1b76f4baa119aec76d66a6c7244",
        runtime.PROFILE: "e47f9a23d1553015a54b0f097e97c34e6e0b4a6bc6eb17b155cd33b6efb2064f",
    }
    assert runtime.startup_runtime_input(PROFILE) is None
    for profile, expected in legacy_hashes.items():
        # This fixture pins the historical policy, before the separately
        # versioned native-JSON observation contract for new intelligence runs.
        assert digest(json_bytes(runtime.policy_manifest(profile, response_observation_contract=""))) == expected
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    accepted = runner.submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    assert wait_terminal(state, accepted["run_id"])["status"] == "completed_degraded"
    run = state / "runs" / accepted["run_id"]
    admission, policy, _ = runtime.validate_admission(run)
    assert set(admission) == {"schema", "run_id", "clock", "parent", "request_sha256", "policy_sha256", "source_sha256"}
    assert policy["business_deadline_seconds"] == 2400 and "runtime_input_sha256" not in policy
    assert not (run / "runtime-input.json").exists()
    assert "runtime_input" not in read_json(run, "receipt.json")
    monkeypatch.setenv(runtime.RUNTIME_POLICY_ENV, str(tmp_path / "missing-current-config.json"))
    before = {str(p.relative_to(state)): digest(p.read_bytes()) for p in state.rglob("*") if p.is_file()}
    verified = runner.execute("verify", {"run_id": run.name}, mode="readonly", state=state)
    assert verified["operation_ok"] and verified["verification"]["ok"]
    assert before == {str(p.relative_to(state)): digest(p.read_bytes()) for p in state.rglob("*") if p.is_file()}


def test_inspection_is_readonly_and_exposes_effective_value_without_startup_path(tmp_path, monkeypatch):
    path = configured(tmp_path, monkeypatch)
    state = tmp_path / "not-created"
    before = path.read_bytes()
    doctor = runner.execute("doctor", {}, mode="readonly", state=state)
    describe = runner.execute("describe", {"profile": PROFILE}, mode="readonly", state=state)
    assert doctor["data"]["business_runtime"]["business_deadline_seconds"] == 5400
    assert describe["data"]["profile"]["business_deadline_seconds"] == 5400
    assert str(path).encode() not in json_bytes(doctor) + json_bytes(describe)
    assert not state.exists() and path.read_bytes() == before


def test_frozen_contract_reads_without_current_configuration_or_new_directories(tmp_path, monkeypatch):
    _state, run, admission, policy, source = prepared(tmp_path, monkeypatch)
    configured_path = Path(os.environ[runtime.RUNTIME_POLICY_ENV])
    configured_path.unlink()
    monkeypatch.setenv(runtime.RUNTIME_POLICY_ENV, str(tmp_path / "unavailable-current.json"))
    before = {p.relative_to(tmp_path).as_posix(): digest(p.read_bytes())
              for p in tmp_path.rglob("*") if p.is_file()}
    observed_admission, observed_policy, observed_source = runtime.validate_admission(run)
    assert observed_admission == admission and observed_source == source
    assert json_bytes(observed_policy) == json_bytes(policy)
    after = {p.relative_to(tmp_path).as_posix(): digest(p.read_bytes())
             for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("mutation", ["packet", "missing_packet", "noncanonical", "admission_hash", "clock", "policy"])
def test_frozen_runtime_policy_clock_and_hash_tampering_are_rejected(tmp_path, monkeypatch, mutation):
    _state, run, admission, policy, _source = prepared(tmp_path, monkeypatch)
    if mutation == "packet":
        packet = read_json(run, "runtime-input.json")
        packet["business_deadline_seconds"] = 7200
        atomic_json(run, "runtime-input.json", packet)
    elif mutation == "missing_packet":
        (run / "runtime-input.json").unlink()
    elif mutation == "noncanonical":
        (run / "runtime-input.json").write_bytes(read_bytes(run, "runtime-input.json") + b" ")
    elif mutation == "admission_hash":
        admission["runtime_input_sha256"] = "0" * 64
        atomic_json(run, "admission.json", admission)
    elif mutation == "clock":
        admission["clock"]["deadline_at_utc"] = (datetime.fromisoformat(
            admission["clock"]["deadline_at_utc"]) + timedelta(seconds=600)).isoformat()
        atomic_json(run, "admission.json", admission)
    else:
        policy["business_deadline_seconds"] = 7200
        atomic_json(run, "policy.json", policy)
        admission["policy_sha256"] = digest(json_bytes(policy))
        atomic_json(run, "admission.json", admission)
    with pytest.raises(EvidenceInvalid):
        runtime.validate_admission(run)


def test_live_submission_counts_queue_and_preparation_and_replay_ignores_current_file(tmp_path, monkeypatch):
    path = configured(tmp_path, monkeypatch)
    original_lock, original_freeze = runner.admission_lock, runner.freeze_source
    order = []
    @contextmanager
    def queue(*args, **kwargs):
        order.append(datetime.now(timezone.utc))
        time.sleep(.04)
        with original_lock(*args, **kwargs):
            yield
    def prepare(*args, **kwargs):
        order.append(datetime.now(timezone.utc))
        time.sleep(.04)
        return original_freeze(*args, **kwargs)
    monkeypatch.setattr(runner, "admission_lock", queue)
    monkeypatch.setattr(runner, "freeze_source", prepare)
    state = tmp_path / "state"
    accepted = runner.submit(state, V2_REQUEST)
    assert accepted["operation_ok"], accepted
    terminal = wait_terminal(state, accepted["run_id"])
    assert terminal["status"] == "failed" and terminal["error"]["code"] == "MODEL_REQUIRED_FAILED"
    run = state / "runs" / accepted["run_id"]
    admission, policy, _source = runtime.validate_admission(run)
    started = datetime.fromisoformat(admission["clock"]["admitted_at_utc"])
    deadline = datetime.fromisoformat(admission["clock"]["deadline_at_utc"])
    assert started <= order[0] < order[1] and (order[1] - started).total_seconds() >= .04
    assert (deadline - started).total_seconds() == policy["business_deadline_seconds"] == 5400
    assert admission["runtime_input_sha256"] == policy["runtime_input_sha256"]
    path.unlink()
    replay = runner.submit(state, V2_REQUEST)
    assert replay["run_id"] == accepted["run_id"] and replay["data"]["idempotent_replay"]
    assert replay["error"]["code"] == "MODEL_REQUIRED_FAILED"
    assert runtime.pointer_read(state) is None
    assert len(order) == 3  # replay waits only for the short submission lock


def test_expired_original_entry_clock_cannot_restart_budget_after_preparation(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    entry = runtime.frozen_clock(datetime.now(timezone.utc) - timedelta(seconds=5401))
    state = tmp_path / "state"
    failed = runner.submit(state, V2_REQUEST, _entry_clock=entry)
    assert failed["error"]["code"] == "TIMEOUT" and failed["error"]["stage"] == "admission.prepare"
    run = state / "runs" / failed["run_id"]
    assert not (run / "admission.json").exists() and not (run / "processes.json").exists()
    assert runtime.pointer_read(state) is None


def test_thread_preparation_preserves_parent_entry_clock_exactly(tmp_path):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    clock = runtime.frozen_clock(datetime.now(timezone.utc) - timedelta(seconds=10))
    with ThreadPoolExecutor(max_workers=1) as pool:
        accepted = pool.submit(runner.submit, state, REQUEST, source=source, _entry_clock=clock).result(timeout=20)
    assert accepted["operation_ok"], accepted
    assert wait_terminal(state, accepted["run_id"])["status"] == "completed_degraded"
    assert read_json(state / "runs" / accepted["run_id"], "admission.json")["clock"] == clock


def test_thread_preparation_inherits_trusted_configuration_and_original_parent_time(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    state = tmp_path / "state"
    entry = runtime.frozen_clock(datetime.now(timezone.utc) - timedelta(seconds=10))
    with ThreadPoolExecutor(max_workers=1) as pool:
        accepted = pool.submit(runner.submit, state, V2_REQUEST, _entry_clock=entry).result(timeout=20)
    assert accepted["operation_ok"], accepted
    terminal = wait_terminal(state, accepted["run_id"])
    assert terminal["status"] == "failed" and terminal["error"]["code"] == "MODEL_REQUIRED_FAILED"
    run = state / "runs" / accepted["run_id"]
    admission, policy, _source = runtime.validate_admission(run)
    assert admission["clock"]["admitted_at_utc"] == entry["admitted_at_utc"]
    assert policy["business_deadline_seconds"] == 5400
    assert (datetime.fromisoformat(admission["clock"]["deadline_at_utc"]) - datetime.fromisoformat(
        admission["clock"]["admitted_at_utc"])).total_seconds() == 5400


def cli_process(tmp_path, request, source, *, seconds=45):
    """A real stdin pipe stays open until the test deliberately supplies EOF."""
    marker = tmp_path / "entry-clock.json"
    bootstrap = textwrap.dedent('''
        import json, pathlib, sys
        sys.path.insert(0, sys.argv[1])
        from trading_system import agent_runner as runner
        source, marker, seconds, state = sys.argv[2:]
        original_clock, original_submit = runner.frozen_clock, runner.submit
        def clock(*args, **kwargs):
            value = original_clock(*args, **kwargs)
            pathlib.Path(marker).write_text(json.dumps(value))
            return value
        def submit(state, request, **kwargs):
            return original_submit(state, request, source=pathlib.Path(source), **kwargs)
        runner.frozen_clock, runner.submit = clock, submit
        runner.PREPARATION_SECONDS = float(seconds)
        raise SystemExit(runner.main(["--mode", "paper", "--state-root", state, "run"]))
    ''')
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", bootstrap,
        str(runtime.SOURCE_ROOT), str(source), str(marker), str(seconds), str(tmp_path / "state")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    started = time.monotonic()
    while not marker.exists():
        assert process.poll() is None and time.monotonic() - started < 5
        time.sleep(.01)
    return process, marker


@pytest.mark.parametrize("seconds", [None, 5400])
def test_cli_captures_business_clock_before_real_slow_stdin(tmp_path, monkeypatch, seconds):
    if seconds is None:
        source, request = fixture_source(tmp_path / "source"), REQUEST
    else:
        configured(tmp_path, monkeypatch, seconds)
        source, request = runtime.SOURCE_ROOT, V2_REQUEST
    process, marker = cli_process(tmp_path, request, source)
    try:
        entry = json.loads(marker.read_text())
        time.sleep(.15)
        sent_at = datetime.now(timezone.utc)
        output, errors = process.communicate(json_bytes(request), timeout=20)
        accepted = json.loads(output)
        assert process.returncode == 0, errors.decode()
        assert accepted["operation_ok"], accepted
        state = tmp_path / "state"
        terminal = wait_terminal(state, accepted["run_id"])
        assert terminal["status"] == ("completed_degraded" if seconds is None else "failed")
        admission, policy, _source = runtime.validate_admission(state / "runs" / accepted["run_id"])
        started = datetime.fromisoformat(admission["clock"]["admitted_at_utc"])
        assert admission["clock"]["admitted_at_utc"] == entry["admitted_at_utc"]
        assert (sent_at - started).total_seconds() >= .15
        assert (datetime.fromisoformat(admission["clock"]["deadline_at_utc"]) - started).total_seconds() == (seconds or 2400)
        assert policy["business_deadline_seconds"] == (seconds or 2400)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_cli_blocked_stdin_consumes_existing_preparation_lease(tmp_path):
    process, _marker = cli_process(tmp_path, REQUEST, runtime.SOURCE_ROOT, seconds=.12)
    try:
        process.wait(timeout=3)  # Keep stdin open; the child must terminate itself.
        output, errors = process.communicate(timeout=3)
        value = json.loads(output)
        assert process.returncode == 9, errors.decode()
        assert value["error"]["code"] == "TIMEOUT" and value["error"]["stage"] == "admission.prepare"
        assert value["run_id"] is None and not (tmp_path / "state").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize("configured_seconds,minimum,maximum", [(5400, 4900, 5001), (2400, 2399, 2401)])
def test_supervisor_uses_validated_remaining_runtime_without_hard_2400_cap(
        tmp_path, monkeypatch, configured_seconds, minimum, maximum):
    state = checked_root(tmp_path / "state", create=True)
    run = checked_root(state / "runs" / ("run-" + "d" * 32), create=True)
    account_fd = runner._lock_fd(state, "account.lock", create=True, nonblocking=True)
    run_fd = runner._lock_fd(run, "run.lock", create=True, nonblocking=True)
    ack_read, ack_write = os.pipe()
    atomic_json(run, "admission.json", {"fixture": "TEST_FIXTURE"})
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5000)
    monkeypatch.setattr(supervisor, "validate_admission", lambda _run: (
        {"clock": {"deadline_at_utc": deadline.isoformat()}},
        {"business_deadline_seconds": configured_seconds}, {}))
    observed = []
    def business(*_args, **kwargs):
        remaining = kwargs["deadline"] - time.monotonic()
        observed.append(remaining)
        assert minimum < remaining < maximum
        raise EvidenceInvalid("RUN_INCOMPLETE", "TEST_FIXTURE stop after observing actual watchdog")
    monkeypatch.setattr(supervisor, "supervise_group", business)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert supervisor.supervisor(state, run.name, account_fd=account_fd, run_fd=run_fd, ack_fd=ack_write) == 1
        assert len(observed) == 1 and runtime.pointer_read(state) is None
    finally:
        os.close(ack_read)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
