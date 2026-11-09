"""Actual loopback sockets exercise idle, heartbeat and absolute wall limits."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import signal
import threading
import time

import pytest

from trading_system.intelligence.catalog import SourceCatalog
from trading_system.intelligence.contracts import IntelligenceError, canonical, utc_now
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.topic import TopicExecution, TopicModelClient, TopicStore, trusted_policy
from trading_system.intelligence.topic_transport import CONTRACT
from trading_system.llm import deepseek_required as transport
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


@contextmanager
def server(mode):
    state = {"requests": [], "closed": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(request)
            body = canonical({"id": "TEST_FIXTURE-native", "object": "chat.completion", "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"answer":"TEST_FIXTURE"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                    "completion_tokens_details": {"reasoning_tokens": 20}}})
            try:
                if mode in {"delay_headers", "parent_alarm"}:
                    time.sleep(.35 if mode == "parent_alarm" else .18)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + (40 if mode == "heartbeat" else 0)))
                self.end_headers()
                if mode == "delay_body":
                    time.sleep(.18)
                if mode == "heartbeat":
                    for _ in range(40):
                        self.wfile.write(b"\n"); self.wfile.flush(); time.sleep(.02)
                if mode == "bad_envelope":
                    body = b"x" * len(body)
                self.wfile.write(body); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as error:
                state["closed"].append(type(error).__name__)

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=service.serve_forever, daemon=True); worker.start()
    try:
        yield "http://127.0.0.1:" + str(service.server_port), state
    finally:
        service.shutdown(); service.server_close(); worker.join(timeout=2)


def client(tmp_path, monkeypatch, endpoint, *, legacy=False, stage="decision.debate", injected_idle=None):
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("LLM_BASE_URL", transport.BASE_URL)
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LLM_API_KEY", "TEST_FIXTURE-transport-key")
    post = transport.requests.post
    def local_post(url, **kwargs):
        assert url == transport.BASE_URL + "/chat/completions"
        if injected_idle is not None:
            # Test-only fault injection separates the two deadlines. Preserve
            # the exact production timeout assertion before altering the socket.
            assert kwargs["timeout"][1] == transport.CHAT_RESPONSE_DEADLINE
            kwargs["timeout"] = (kwargs["timeout"][0], injected_idle)
        return post(endpoint + "/chat/completions", **kwargs)
    monkeypatch.setattr(transport.requests, "post", local_post)
    registry = SourceRegistry([SourceSpec("TEST_FIXTURE", "page", "https://example.invalid/original",
        allowed_hosts=("example.invalid",))])
    catalog = SourceCatalog(registry, reviewed_entities={})
    policy = trusted_policy()
    if legacy:
        policy.pop("model_transport_contract"); policy["version"] = "2026-11-07.3"
    topic = {"topic_id": "TEST_FIXTURE-topic", "version": 1, "catalog_sha256": catalog.manifest()["sha256"],
        "question": "TEST_FIXTURE full original", "entity_ids": [], "material_policy": {"news_events": "required"}}
    store = TopicStore(tmp_path / "state")
    job = store.create({"topic_id": topic["topic_id"], "version": 1, "idempotency_key": "TEST_FIXTURE-run"},
        topic, policy, catalog=catalog)
    execution = TopicExecution(store, job["job_id"]); execution._active.spec.name = stage
    return TopicModelClient(execution), execution, job


def complete(instance):
    return instance.complete_json(system="TEST_FIXTURE system", user="TEST_FIXTURE user", schema_hint={},
        schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False},
        public_payload={"original": "TEST_FIXTURE whole original"})


def failures(execution):
    with execution.store.connection() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT record FROM topic_events WHERE job_id=? AND kind='model_failure_observed' ORDER BY sequence",
            (execution.job_id,))]


@pytest.mark.parametrize("mode", ["delay_headers", "delay_body"])
def test_new_admitted_idle_can_receive_first_byte_after_old_idle(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(transport, "CHAT_READ_TIMEOUT", .04)
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .6)
    with server(mode) as (endpoint, state):
        instance, execution, _ = client(tmp_path, monkeypatch, endpoint)
        assert complete(instance) == {"answer": "TEST_FIXTURE"}
        rows = execution.store.attempts(execution.job_id)
        assert len(state["requests"]) == len(rows) == 1 and rows[0]["state"] == "settled"
        assert rows[0]["receipt"]["usage"]["total_tokens"] == 140 and not failures(execution)
        assert state["requests"][0]["max_tokens"] == 393216


def test_legacy_idle_still_times_out_without_new_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CHAT_READ_TIMEOUT", .04)
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .6)
    with server("delay_headers") as (endpoint, state):
        instance, execution, _ = client(tmp_path, monkeypatch, endpoint, legacy=True)
        with pytest.raises(RequiredModelUnavailable) as stopped: complete(instance)
        assert stopped.value.diagnostic["code"] == "NETWORK_TIMEOUT"
        assert len(state["requests"]) == 1 and execution.store.attempts(execution.job_id)[0]["state"] == "unknown"
        assert not failures(execution)


def test_heartbeat_does_not_extend_total_and_unknown_cannot_send_again(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CHAT_READ_TIMEOUT", .04)
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .18)
    with server("heartbeat") as (endpoint, state):
        instance, execution, job = client(tmp_path, monkeypatch, endpoint)
        started = time.monotonic()
        with pytest.raises(RequiredModelUnavailable) as stopped: complete(instance)
        elapsed = time.monotonic() - started
        assert stopped.value.diagnostic["code"] == "RESPONSE_DEADLINE" and elapsed < .5
        observation = failures(execution)[0]
        assert observation["code"] == "RESPONSE_DEADLINE" and observation["deadline_owner"] == "response"
        assert observation["phase"] == "response_body" and observation["transport"]["http_status"] == 200
        assert observation["transport"]["body_bytes"] > 0 and observation["transport"]["body_whitespace_only"]
        assert not observation["transport"]["body_complete"] and observation["usage_state"] == "unknown"
        assert observation["schema"] == CONTRACT
        with pytest.raises(IntelligenceError) as denied:
            complete(TopicModelClient(execution))
        assert denied.value.code == "USAGE_RECONCILIATION_REQUIRED" and len(state["requests"]) == 1
        with pytest.raises(IntelligenceError) as reconcile:
            TopicModelClient.reconcile_pending(execution)
        assert reconcile.value.code == "USAGE_RECONCILIATION_REQUIRED"
        assert len(execution.store.attempts(job["job_id"])) == 1


def test_bad_envelope_records_only_closed_body_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .6)
    with server("bad_envelope") as (endpoint, state):
        instance, execution, _ = client(tmp_path, monkeypatch, endpoint)
        with pytest.raises(RequiredModelUnavailable) as stopped: complete(instance)
        assert stopped.value.diagnostic["code"] == "INVALID_RESPONSE_JSON"
        observation = failures(execution)[0]
        assert observation["phase"] == "http_envelope" and observation["transport"]["body_complete"]
        assert observation["transport"]["body_sha256"] == hashlib.sha256(b"x" * observation["transport"]["body_bytes"]).hexdigest()
        serialized = canonical(observation).decode()
        assert "TEST_FIXTURE-transport-key" not in serialized and "reasoning_content" not in serialized
        assert "body" not in observation and "response" not in observation and len(state["requests"]) == 1


def test_absolute_deadline_also_bounds_headers_before_any_body(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .08)
    with server("delay_headers") as (endpoint, state):
        instance, execution, _ = client(tmp_path, monkeypatch, endpoint)
        started = time.monotonic()
        with pytest.raises(RequiredModelUnavailable) as stopped: complete(instance)
        # Idle and absolute timers have the same ceiling here. Either may win
        # the real socket race; both must stop and identify the observed phase.
        assert stopped.value.diagnostic["code"] in {"RESPONSE_DEADLINE", "NETWORK_TIMEOUT"}
        assert time.monotonic() - started < .4
        observed = failures(execution)[0]
        assert observed["code"] == stopped.value.diagnostic["code"]
        assert (observed["deadline_owner"] == "response" if observed["code"] == "RESPONSE_DEADLINE"
            else observed["transport_error"] == "read_timeout")
        assert observed["phase"] == "awaiting_headers" and observed["transport"]["http_status"] is None
        assert observed["transport"]["body_bytes"] == 0 and observed["usage_state"] == "unknown"
        assert not observed["transport"]["body_complete"] and len(state["requests"]) == 1


def test_real_earlier_parent_alarm_is_not_swallowed_or_rearmed(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .6)
    fired = []
    def parent(_number, _frame):
        fired.append(True); raise IntelligenceError("WALL_DEADLINE")
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    assert old_timer == (0.0, 0.0)
    try:
        signal.signal(signal.SIGALRM, parent)
        with server("parent_alarm") as (endpoint, state):
            instance, execution, _ = client(tmp_path, monkeypatch, endpoint)
            signal.setitimer(signal.ITIMER_REAL, .12)
            with pytest.raises(IntelligenceError) as stopped: complete(instance)
            assert stopped.value.code == "WALL_DEADLINE" and len(fired) == 1
            assert signal.getsignal(signal.SIGALRM) is parent and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
            observation = failures(execution)[0]
            assert observation["code"] == "WALL_DEADLINE" and observation["deadline_owner"] == "parent_watchdog"
            assert execution.store.attempts(execution.job_id)[0]["state"] == "unknown"
            with pytest.raises(IntelligenceError): complete(TopicModelClient(execution))
            assert len(state["requests"]) == 1 and len(fired) == 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0); signal.signal(signal.SIGALRM, old_handler)


def test_headers_absolute_alarm_fires_with_socket_idle_deliberately_longer(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CHAT_RESPONSE_DEADLINE", .08)
    with server("delay_headers") as (endpoint, state):
        instance, execution, _ = client(tmp_path, monkeypatch, endpoint, injected_idle=.6)
        with pytest.raises(RequiredModelUnavailable) as stopped: complete(instance)
        assert stopped.value.diagnostic["code"] == "RESPONSE_DEADLINE"
        observed = failures(execution)[0]
        assert observed["phase"] == "awaiting_headers" and observed["deadline_owner"] == "response"
        assert observed["effective_deadline_seconds"] == .08
        assert observed["transport"]["http_status"] is None and not observed["transport"]["body_complete"]
        assert observed["usage_state"] == "unknown" and len(state["requests"]) == 1
