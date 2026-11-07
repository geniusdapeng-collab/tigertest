"""Independent transport mechanism negatives; fixture-only, no provider/network.

The store tests exercise real SQLite transactions and immutable events. Their
_get fixture supplies an already admitted identity so catalog/acquisition and
paid semantics are outside this mechanism suite, not silently mocked success.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import signal
import sqlite3
import time

import pytest
import requests

from trading_system.intelligence import topic as module
from trading_system.intelligence import topic_transport as transport
from trading_system.intelligence.contracts import IntelligenceError, canonical, digest, timestamp, utc_now
from trading_system.llm import deepseek_required as base
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("TEST_FIXTURE network forbidden")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "TEST_FIXTURE_TRANSPORT_KEY"}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def alarm_state():
    original_handler = signal.getsignal(signal.SIGALRM)
    original_delay, original_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)
        if original_delay:
            signal.setitimer(signal.ITIMER_REAL,
                max(0.000001, original_delay - (time.monotonic() - started)), original_interval)


def policy_09aa():
    value = {k: deepcopy(v) for k, v in module.DEFAULT_POLICY.items() if k != "model_transport_contract"}
    value["version"] = "2026-11-07.3"
    return module.trusted_policy_value(value)


def fixture_job(policy=None):
    policy = module.trusted_policy_value(deepcopy(module.DEFAULT_POLICY)) if policy is None else policy
    return {"tenant": "public", "job_id": "topicrun_" + "a" * 32,
        "request_sha256": "1" * 64, "policy": policy, "policy_sha256": digest(policy),
        "source_fingerprint": {"sha256": "2" * 64},
        "deadline": timestamp(utc_now() + timedelta(seconds=2400))}


def fixture_request():
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": "TEST_FIXTURE same wire"}],
        "thinking": {"type": "enabled"}, "reasoning_effort": "high", "max_tokens": 393216,
        "response_format": {"type": "json_object"}, "stream": False}


def fixture_store(tmp_path, monkeypatch, policy=None):
    job = fixture_job(policy)
    store = module.TopicStore(tmp_path / "state")
    # Deliberately isolate admitted catalog/source identity, not the tested
    # transaction, row validation, usage hold or event persistence.
    monkeypatch.setattr(store, "_get", lambda _db, job_id: deepcopy(job) if job_id == job["job_id"] else None)
    store.directory(job["job_id"]).mkdir()
    return store, job


def fixture_observation(job, attempt):
    observed = transport.TransportObservation()
    observed.response_seconds = 480.0
    observed.effective_seconds = 480.0
    return observed.failure(job, attempt["attempt_id"], attempt["request_sha"], attempt["stage"],
        RequiredModelUnavailable("NETWORK_TIMEOUT"))


def fixture_attempt():
    return {"attempt_id": "attempt_" + "b" * 32, "request_sha": "3" * 64,
        "stage": "sector.narrative"}


def resigned(value):
    value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
    return value


def event_rows(store):
    with store.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM topic_events ORDER BY sequence")]


def test_09aa_absent_transport_policy_preserves_original_be1b_digest():
    policy = policy_09aa()
    assert "model_transport_contract" not in policy
    assert digest(policy) == "be1b1c5fee67211bb7a23ef0677eb465c2f2a6b98e93723d492702cc6e981185"
    none_policy = module.trusted_policy_value({**policy, "model_transport_contract": None})
    assert none_policy == {**policy, "model_transport_contract": None}
    assert digest(none_policy) == digest({**policy, "model_transport_contract": None})
    assert digest(none_policy) != digest(policy)


@pytest.mark.parametrize("contract", ["unknown", "", True, 1, [], {}, [transport.CONTRACT]])
def test_unknown_transport_contract_rejected_before_model_admission(contract):
    with pytest.raises(IntelligenceError) as denied:
        module.trusted_policy_value({**module.DEFAULT_POLICY, "model_transport_contract": contract})
    assert denied.value.code == "INVALID_STARTUP"


class FixtureResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self):
        self.raw = io.BytesIO(canonical({"id": "TEST_FIXTURE-native", "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}}))

    def close(self):
        self.raw.close()


@pytest.mark.parametrize("legacy", ["missing", None])
def test_legacy_same_wire_keeps_original_180_timeout_and_no_new_guard(tmp_path, monkeypatch, legacy):
    policy = policy_09aa()
    if legacy is None:
        policy["model_transport_contract"] = None
    store, job = fixture_store(tmp_path, monkeypatch, policy)
    execution = module.TopicExecution(store, job["job_id"])
    client = module.TopicModelClient(execution)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return FixtureResponse()
    monkeypatch.setattr(requests, "post", post)
    def guard_forbidden(*_args, **_kwargs):
        raise AssertionError("Legacy must retain its original no-new-watchdog branch")
    monkeypatch.setattr(module, "TransportObservation", guard_forbidden)
    wire = fixture_request()
    before = canonical(wire)
    result, _ = client._post_reserved("/chat/completions", wire)
    assert client._topic_transport is False and result["id"] == "TEST_FIXTURE-native"
    assert canonical(wire) == before == calls[0][1]["data"]
    assert calls[0][0] == "https://api.deepseek.com/chat/completions"
    assert calls[0][1]["timeout"] == (base.CONNECT_TIMEOUT, 180.0)
    assert calls[0][1]["verify"] is True and calls[0][1]["allow_redirects"] is False
    assert [row["kind"] for row in event_rows(store)] == ["model_dispatched", "model_settled"]


@pytest.mark.parametrize("field,wrong", [
    ("tenant", "foreign"), ("job_id", "topicrun_" + "c" * 32),
    ("job_request_sha256", "4" * 64), ("policy_sha256", "5" * 64),
    ("source_fingerprint_sha256", "6" * 64), ("attempt_id", "attempt_" + "d" * 32),
    ("request_sha256", "7" * 64), ("stage", "decision.debate"),
    ("schema", "foreign/v1"), ("phase", "provider_text"), ("code", "private body"),
    ("transport_error", "private exception"), ("deadline_owner", "foreign"),
    ("elapsed_seconds", -1), ("elapsed_seconds", True),
    ("response_deadline_seconds", 0), ("effective_deadline_seconds", 481),
    ("body_scope", "complete_provider_body"), ("usage_state", "zero"),
])
def test_closed_failure_rejects_coherently_rehashed_identity_and_field_tampering(field, wrong):
    job, attempt = fixture_job(), fixture_attempt()
    value = fixture_observation(job, attempt)
    value[field] = wrong
    resigned(value)
    with pytest.raises(IntelligenceError) as denied:
        transport.validate_failure(value, job, attempt)
    assert denied.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("fault", ["extra_field", "missing_field", "bad_hash", "nested_extra", "nested_negative_bytes",
    "nested_bool_bytes", "nested_invalid_status", "legacy_policy"])
def test_failure_observation_is_closed_and_bound_to_admitted_contract(fault):
    job, attempt = fixture_job(), fixture_attempt()
    value = fixture_observation(job, attempt)
    if fault == "extra_field": value["provider_body"] = "TEST_FIXTURE_SECRET_CANARY"
    elif fault == "missing_field": value.pop("phase")
    elif fault == "bad_hash": value["sha256"] = "0" * 64
    elif fault == "nested_extra": value["transport"]["authorization"] = "TEST_FIXTURE_SECRET_CANARY"
    elif fault == "nested_negative_bytes": value["transport"]["body_bytes"] = -1
    elif fault == "nested_bool_bytes": value["transport"]["body_bytes"] = True
    elif fault == "nested_invalid_status": value["transport"]["http_status"] = 999
    elif fault == "legacy_policy": job["policy"].pop("model_transport_contract")
    if fault != "bad_hash": resigned(value)
    with pytest.raises(IntelligenceError) as denied:
        transport.validate_failure(value, job, attempt)
    assert denied.value.code == "HASH_MISMATCH"
    assert "TEST_FIXTURE_SECRET_CANARY" not in str(denied.value)


def test_failure_never_copies_exception_text_and_returns_detached_closed_value():
    job, attempt = fixture_job(), fixture_attempt()
    observation = transport.TransportObservation()
    observation.response_seconds, observation.effective_seconds = 480, 30
    value = observation.failure(job, attempt["attempt_id"], attempt["request_sha"], attempt["stage"],
        RuntimeError("TEST_FIXTURE_BODY_REASONING_AUTH_CANARY"))
    assert value["code"] == "INTERNAL_ERROR" and value["usage_state"] == "unknown"
    assert "TEST_FIXTURE_BODY_REASONING_AUTH_CANARY" not in canonical(value).decode()
    validated = transport.validate_failure(value, job, attempt)
    validated["transport"]["body_bytes"] = 20
    assert value["transport"]["body_bytes"] == 0


@pytest.mark.parametrize("field", ["elapsed_seconds", "response_deadline_seconds", "effective_deadline_seconds"])
def test_failure_numeric_overflow_is_closed_hash_mismatch(field):
    job, attempt = fixture_job(), fixture_attempt()
    value = fixture_observation(job, attempt)
    value[field] = 10 ** 1000
    resigned(value)
    with pytest.raises(IntelligenceError) as denied:
        transport.validate_failure(value, job, attempt)
    assert denied.value.code == "HASH_MISMATCH"


def test_unknown_and_observation_commit_together_and_events_are_append_only(tmp_path, monkeypatch):
    store, job = fixture_store(tmp_path, monkeypatch)
    aid = store.reserve(job["job_id"], "sector.narrative", fixture_request())
    attempt = store.attempts(job["job_id"])[0]
    value = fixture_observation(job, attempt)
    store.settle(job["job_id"], aid, None, failure_observation=value)
    assert store.attempts(job["job_id"])[0]["state"] == "unknown"
    assert store.attempts(job["job_id"])[0]["receipt"] is None
    events = event_rows(store)
    assert [row["kind"] for row in events] == ["model_dispatched", "model_unknown", "model_failure_observed"]
    assert json.loads(events[-1]["record"]) == value
    with sqlite3.connect(store.database) as db:
        for statement in ["UPDATE topic_events SET kind='forged' WHERE sequence=?", "DELETE FROM topic_events WHERE sequence=?"]:
            with pytest.raises(sqlite3.IntegrityError, match="immutable topic event"):
                db.execute(statement, (events[-1]["sequence"],))
    assert event_rows(store) == events
    with pytest.raises(IntelligenceError) as denied:
        store.settle(job["job_id"], aid, None, failure_observation=value)
    assert denied.value.code == "REVISION_CONFLICT" and event_rows(store) == events


def test_second_event_write_failure_rolls_back_unknown_update_and_first_event(tmp_path, monkeypatch):
    store, job = fixture_store(tmp_path, monkeypatch)
    aid = store.reserve(job["job_id"], "sector.narrative", fixture_request())
    attempts, events = store.attempts(job["job_id"]), event_rows(store)
    original_event = store.event
    def interrupted(db, job_id, kind, value):
        if kind == "model_failure_observed":
            raise sqlite3.IntegrityError("TEST_FIXTURE fail second event")
        original_event(db, job_id, kind, value)
    monkeypatch.setattr(store, "event", interrupted)
    with pytest.raises(IntelligenceError) as denied:
        store.settle(job["job_id"], aid, None, failure_observation=fixture_observation(job, attempts[0]))
    assert denied.value.code == "ARCHIVE_UNAVAILABLE"
    assert store.attempts(job["job_id"]) == attempts and event_rows(store) == events


@pytest.mark.parametrize("field", ["attempt_id", "job_id", "stage", "request_sha256", "tenant", "sha256"])
def test_invalid_observation_never_mutates_dispatched_hold_or_events(tmp_path, monkeypatch, field):
    store, job = fixture_store(tmp_path, monkeypatch)
    aid = store.reserve(job["job_id"], "sector.narrative", fixture_request())
    before, events = store.attempts(job["job_id"]), event_rows(store)
    value = fixture_observation(job, before[0])
    value[field] = "TEST_FIXTURE-wrong"
    if field != "sha256": resigned(value)
    with pytest.raises(IntelligenceError) as denied:
        store.settle(job["job_id"], aid, None, failure_observation=value)
    assert denied.value.code == "HASH_MISMATCH"
    assert store.attempts(job["job_id"]) == before and event_rows(store) == events


def test_failure_observation_cannot_replace_known_usage_receipt(tmp_path, monkeypatch):
    store, job = fixture_store(tmp_path, monkeypatch)
    aid = store.reserve(job["job_id"], "sector.narrative", fixture_request())
    before, events = store.attempts(job["job_id"]), event_rows(store)
    with pytest.raises(IntelligenceError) as denied:
        store.settle(job["job_id"], aid, {"usage": {"total_tokens": 10}},
            failure_observation=fixture_observation(job, before[0]))
    assert denied.value.code == "HASH_MISMATCH"
    assert store.attempts(job["job_id"]) == before and event_rows(store) == events


def test_earlier_parent_alarm_preserves_exact_error_handler_and_fires_once(alarm_state):
    original = IntelligenceError("WALL_DEADLINE", "TEST_FIXTURE original outer reason")
    fired = []
    def parent(number, _frame):
        fired.append(number)
        raise original
    signal.signal(signal.SIGALRM, parent)
    signal.setitimer(signal.ITIMER_REAL, 0.025)
    observation = transport.TransportObservation()
    with pytest.raises(IntelligenceError) as denied:
        with observation.deadline(fixture_job(), 0.2, "decision.debate"):
            time.sleep(0.1)
    assert denied.value is original and observation.deadline_owner == "parent_watchdog"
    assert signal.getsignal(signal.SIGALRM) is parent and len(fired) == 1
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    time.sleep(0.005)
    assert len(fired) == 1


def test_returning_parent_alarm_still_stops_guard_without_refiring(alarm_state):
    fired = []
    def parent(number, _frame): fired.append(number)
    signal.signal(signal.SIGALRM, parent)
    signal.setitimer(signal.ITIMER_REAL, 0.02)
    observation = transport.TransportObservation()
    with pytest.raises(IntelligenceError) as denied:
        with observation.deadline(fixture_job(), 0.2, "decision.debate"):
            time.sleep(0.08)
    assert denied.value.code == "WALL_DEADLINE" and fired == [signal.SIGALRM]
    assert signal.getsignal(signal.SIGALRM) is parent and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@pytest.mark.parametrize("interrupt", [False, True])
def test_outer_timer_restoration_subtracts_elapsed_and_does_not_extend_deadline(alarm_state, interrupt):
    def parent(_number, _frame): raise AssertionError("TEST_FIXTURE should finish before outer watchdog")
    signal.signal(signal.SIGALRM, parent)
    signal.setitimer(signal.ITIMER_REAL, 0.8)
    before = signal.getitimer(signal.ITIMER_REAL)[0]
    started = time.monotonic()
    try:
        with transport.TransportObservation().deadline(fixture_job(), 0.4, "decision.debate"):
            time.sleep(0.025)
            if interrupt:
                raise RuntimeError("TEST_FIXTURE independent body error")
    except RuntimeError:
        assert interrupt
    elapsed = time.monotonic() - started
    remaining = signal.getitimer(signal.ITIMER_REAL)[0]
    assert 0 < remaining < before - 0.015
    assert remaining <= before - elapsed + 0.01
    assert remaining >= before - elapsed - 0.02
    assert signal.getsignal(signal.SIGALRM) is parent


def test_private_alarm_crosses_base_broad_exception_handler_and_close_failure(alarm_state, tmp_path, monkeypatch):
    store, job = fixture_store(tmp_path, monkeypatch)
    client = module.TopicModelClient(module.TopicExecution(store, job["job_id"]))
    response = FixtureResponse()
    class BlockingBody:
        def read1(self, _size):
            time.sleep(0.2)
            return b""
    response.raw = BlockingBody()
    def bad_close(): raise RuntimeError("TEST_FIXTURE close masks alarm")
    response.close = bad_close
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: response)
    observation = transport.TransportObservation()
    with pytest.raises(RequiredModelUnavailable) as denied:
        with observation.deadline(job, 0.02, "decision.debate"):
            base.DeepSeekRequiredClient._post_json(client, "/chat/completions", fixture_request(),
                _transport_observation=observation.data, _chat_read_timeout=480)
    assert denied.value.diagnostic["code"] == "RESPONSE_DEADLINE"
    assert observation.deadline_owner == "response" and observation.data["phase"] == "response_body"


@pytest.mark.parametrize("stage,total_remaining,expected", [("sector.narrative", 541, 1), ("decision.debate", 60, 30)])
def test_analysis_and_review_reserves_bound_guard_without_waiting_real_minutes(alarm_state, monkeypatch, stage, total_remaining, expected):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    job = fixture_job()
    job["deadline"] = timestamp(now + timedelta(seconds=total_remaining))
    monkeypatch.setattr(transport, "utc_now", lambda: now)
    observation = transport.TransportObservation()
    with observation.deadline(job, 480, stage):
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= expected and interval == 0
        assert observation.effective_seconds == expected
    assert job["policy"]["review_reserve_seconds"] == 510 and job["policy"]["closeout_seconds"] == 30


def test_exhausted_analysis_guard_has_valid_zero_effective_failure_without_entering_body(alarm_state, monkeypatch):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    job, attempt = fixture_job(), fixture_attempt()
    job["deadline"] = timestamp(now + timedelta(seconds=540))
    monkeypatch.setattr(transport, "utc_now", lambda: now)
    observation, entered = transport.TransportObservation(), []
    with pytest.raises(IntelligenceError) as denied:
        with observation.deadline(job, 480, "sector.narrative"):
            entered.append(True)
    assert denied.value.code == "WALL_DEADLINE" and not entered
    value = observation.failure(job, attempt["attempt_id"], attempt["request_sha"], attempt["stage"], denied.value)
    assert value["response_deadline_seconds"] == 480 and value["effective_deadline_seconds"] == 0
    assert value["phase"] == "awaiting_headers" and transport.validate_failure(value, job, attempt) == value
    value["phase"] = "response_body"
    resigned(value)
    with pytest.raises(IntelligenceError) as rejected:
        transport.validate_failure(value, job, attempt)
    assert rejected.value.code == "HASH_MISMATCH"
