"""Versioned native accounting with real durable evidence, synthetic HTTP replies."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

import pytest
import requests

from test_deepseek_required import KEY, SCHEMA, Response, chat_response
from trading_system.agent_evidence import EvidenceStore, read_snapshot
from trading_system.agent_runtime import INTELLIGENCE_PROFILE, policy_manifest, trusted_model_no_proxy, run_environment
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.json_observation import CONTRACT, replay_attempt
from trading_system.redline.execution import (
    ExecutionContext, ExecutionPolicy, RuntimePaths, INTELLIGENCE_POLICY_ID, INTELLIGENCE_SEARCH_SOURCES,
)
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from trading_system.redline.model_diagnostics import validate_model_diagnostic


@pytest.fixture
def observed(tmp_path, monkeypatch):
    for name, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": KEY}.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "run"; root.mkdir()
    work = root / "work"; work.mkdir()
    paths = RuntimePaths(work_dir=work, reports_dir=work / "reports", journal_path=work / "reports/journal.json",
        sim_path=work / "reports/sim_portfolio.json", calibration_path=work / "reports/calibration.json",
        options_history_dir=work / "reports/options", governance_path=work / "reports/governance.jsonl",
        proposals_dir=work / "reports/proposals", calendar_override_path=work / "calendar.json")
    instant = datetime(2026, 11, 6, 14, tzinfo=timezone.utc)
    context = ExecutionContext(run_id="run-" + "a" * 32,
        run_date=instant.astimezone(ZoneInfo("America/New_York")).date(), admitted_at=instant,
        paths=paths, policy=ExecutionPolicy(policy_id=INTELLIGENCE_POLICY_ID, model_mode="enabled",
            search_source_names=INTELLIGENCE_SEARCH_SOURCES), store=EvidenceStore(root, "run-" + "a" * 32),
        response_observation_contract=CONTRACT)
    advance(context, "clean.llm_semantic")
    return context


def advance(context, target):
    while context._specs[context._cursor].name != target:
        with context.step(context._specs[context._cursor].name, {"fixture": "TEST_FIXTURE"}) as scope:
            scope.output({"fixture": "TEST_FIXTURE"})


def native(content, identifier):
    result = chat_response(id=identifier)
    result["choices"][0]["message"]["content"] = content
    return result


def invoke(context, client=None):
    client = client or DeepSeekRequiredClient(context)
    return client.complete_json(system="TEST_FIXTURE JSON", user="Full public original TEST_FIXTURE",
                                schema_hint={}, schema=SCHEMA)


def snapshot(context, ref):
    return read_snapshot(context.store.root, ref, run_id=context.run_id,
                         stage=context._active.spec.name if context._active else "clean.llm_semantic", role="output")


def test_bad_assistant_json_is_durable_before_same_wire_retry(observed, monkeypatch):
    wires = []
    replies = iter([native('{"score":', "native-first"), native('{"score":7,"reason":"ok"}', "native-second")])
    def post(_url, **kwargs):
        wires.append(kwargs["data"])
        if len(wires) == 2:
            saved = [json.loads(p.read_text())["value"] for p in (observed.store.root / "evidence").glob("*.json")]
            first = [v for v in saved if isinstance(v, dict) and v.get("schema") == "tiger.deepseek-observed-attempt/v2"]
            assert len(first) == 1 and first[0]["response"]["choices"][0]["message"]["content"] == '{"score":'
            assert first[0]["response"]["usage"]["total_tokens"] == 50
        return Response(next(replies))
    monkeypatch.setattr(requests, "post", post)
    with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}) as scope:
        result = invoke(observed); scope.output(result)
        call = scope.io_calls[0]
        proof = read_snapshot(observed.store.root, call["output_ref"], run_id=observed.run_id,
                              stage="clean.llm_semantic", role="output")
        assert proof["schema"] == "tiger.deepseek-call.v2"
        assert proof["response_observation"]["retry_used"] is True
        first, second = proof["response_observation"]["attempts"]
        assert first["diagnostic"]["json_stage"] == "assistant_content" and second["diagnostic"] is None
        assert snapshot(observed, first["observation_ref"])["response"]["id"] == "native-first"
    assert result == {"score": 7, "reason": "ok"} and wires[0] == wires[1]
    assert json.loads(wires[0])["max_tokens"] == 393216


@pytest.mark.parametrize("content", ["", " ", "\n\t "])
def test_known_blank_has_usage_and_one_bounded_format_retry(observed, monkeypatch, content):
    replies = iter([native(content, "blank-first"), native('{"score":7,"reason":"ok"}', "blank-second")])
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(k["data"]) or Response(next(replies)))
    with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}) as scope:
        scope.output(invoke(observed))
    assert len(sent) == 2 and sent[0] == sent[1]


@pytest.mark.parametrize("body,whitespace", [(b"", True), (b" \r\n ", True), (b"{bad", False)])
def test_bad_http_envelope_never_retries_and_never_has_zero_usage(observed, monkeypatch, body, whitespace):
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(k["data"]) or Response(body=body))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}):
            invoke(observed)
    diagnostic = error.value.diagnostic
    assert diagnostic["json_stage"] == "http_envelope" and diagnostic["code"] == "INVALID_RESPONSE_JSON"
    assert diagnostic["transport"] == {"http_status": 200, "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_bytes": len(body), "body_whitespace_only": whitespace, "body_complete": True}
    entry = error.value.observation["attempts"][0]
    attempt = snapshot(observed, entry["observation_ref"])
    assert attempt["response"] is None and "usage" not in attempt and len(sent) == 1
    assert attempt["transport_diagnostic"] == diagnostic
    events = [json.loads(line) for line in (observed.store.root / "events.jsonl").read_text().splitlines()]
    failed = read_snapshot(observed.store.root, events[-1]["output_ref"], run_id=observed.run_id,
                           stage="clean.llm_semantic", role="output")
    failure = read_snapshot(observed.store.root, failed["io_calls"][0]["error_ref"], run_id=observed.run_id,
                            stage="clean.llm_semantic", role="output")
    assert events[-1]["status"] == "failed" and failure["response_observation"] == error.value.observation


@pytest.mark.parametrize("change", [{"usage": None}, {"id": ""}, {"model": "other-model"}])
def test_unverified_identity_or_usage_has_one_unknown_attempt(observed, monkeypatch, change):
    sent = []
    payload = native('{"score":', "native-first"); payload.update(change)
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Response(payload))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}): invoke(observed)
    attempt = snapshot(observed, error.value.observation["attempts"][0]["observation_ref"])
    assert attempt["response"] is None and len(sent) == 1


def test_two_known_failures_keep_both_original_usage_records(observed, monkeypatch):
    replies = iter([native('{"score":', "bad-first"), native('{"score":', "bad-second")])
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(next(replies)))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}): invoke(observed)
    attempts = [snapshot(observed, entry["observation_ref"]) for entry in error.value.observation["attempts"]]
    assert [v["response"]["id"] for v in attempts] == ["bad-first", "bad-second"]
    assert sum(v["response"]["usage"]["total_tokens"] for v in attempts) == 100


@pytest.mark.parametrize("first_bad", ['{"score":', '{"score":12,"reason":"public"}'])
def test_retry_budget_is_shared_when_the_client_is_recreated(observed, monkeypatch, first_bad):
    replies = iter([native(first_bad, "bad-first"), native('{"score":7,"reason":"ok"}', "ok-second"),
                    native('{"score":', "bad-third")])
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Response(next(replies)))
    with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}) as scope: scope.output(invoke(observed))
    advance(observed, "tech.sentiment")
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("tech.sentiment", {"fixture": "TEST_FIXTURE"}): invoke(observed, DeepSeekRequiredClient(observed))
    assert len(sent) == 3 and error.value.observation["retry_used"] is False


@pytest.mark.parametrize("usage,code", [
    ({"prompt_tokens": 40, "completion_tokens": 400000, "total_tokens": 400040}, "MODEL_USAGE_EXCEEDS_BUDGET"),
    ({"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50,
      "completion_tokens_details": {"reasoning_tokens": 1}}, "NON_THINKING_OUTPUT_REQUIRED"),
])
def test_known_usage_is_not_clipped_or_lost_on_later_policy_failure(observed, monkeypatch, usage, code):
    payload = native('{"score":7,"reason":"ok"}', "known-policy-fail"); payload["usage"] = usage
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Response(payload))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}): invoke(observed)
    assert error.value.diagnostic["code"] == code and len(sent) == 1
    attempt = snapshot(observed, error.value.observation["attempts"][0]["observation_ref"])
    assert attempt["response"]["usage"] == usage


@pytest.mark.parametrize("encoded", [False, True])
def test_redacted_bad_reply_has_known_usage_but_cannot_authorize_retry(observed, monkeypatch, encoded):
    secret = "".join("\\u%04x" % ord(c) for c in KEY) if encoded else KEY
    text = '{"score": ' + secret + ' secret=private-token'
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Response(native(text, "private-bad")))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}): invoke(observed)
    attempt = snapshot(observed, error.value.observation["attempts"][0]["observation_ref"])
    assert attempt["content_redacted"] is True and len(sent) == 1
    assert attempt["response"]["usage"]["total_tokens"] == 50
    serialized = "\n".join(p.read_text() for p in (observed.store.root / "evidence").glob("*.json"))
    assert KEY not in serialized and "private-token" not in serialized


@pytest.mark.parametrize("kind", ["timeout", "transport", "close"])
def test_uncertain_transport_or_close_never_authorizes_another_send(observed, monkeypatch, kind):
    sent = []
    def post(*a, **k):
        sent.append(1)
        if kind == "timeout": raise requests.Timeout(KEY)
        if kind == "transport": raise OSError(KEY)
        response = Response(native('{"score":7,"reason":"ok"}', "close-failed"))
        def fail_close(): raise OSError(KEY)
        response.close = fail_close
        return response
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}): invoke(observed)
    attempt = snapshot(observed, error.value.observation["attempts"][0]["observation_ref"])
    assert attempt["response"] is None and len(sent) == 1
    assert error.value.diagnostic["json_stage"] == "http_envelope"
    assert KEY not in json.dumps([attempt, error.value.observation, error.value.diagnostic])


def test_durable_write_failure_stops_before_any_retry(observed, monkeypatch):
    from trading_system.redline.execution import EvidenceWriteError
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Response(native('{"score":', "write-failed")))
    def fail_write(value): raise EvidenceWriteError("TEST_FIXTURE native snapshot unavailable")
    monkeypatch.setattr(observed, "model_attempt_snapshot", fail_write)
    with pytest.raises(EvidenceWriteError):
        with observed.step("clean.llm_semantic", {"fixture": "TEST_FIXTURE"}): invoke(observed)
    assert len(sent) == 1


def test_valid_redacted_json_and_private_reasoning_keep_safe_native_only(observed, monkeypatch):
    advance(observed, "tech.risk")
    response = native('{"score":7,"reason":"' + KEY + '"}', "safe-redacted")
    response["choices"][0]["message"]["reasoning_content"] = "TEST_FIXTURE_PRIVATE_REASONING " + KEY
    response["usage"]["completion_tokens_details"] = {"reasoning_tokens": 7}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(response))
    with observed.step("tech.risk", {"fixture": "TEST_FIXTURE"}) as scope:
        result = invoke(observed); scope.output(result)
    assert result == {"score": 7, "reason": "[REDACTED_SECRET]"}
    serialized = "\n".join(p.read_text() for p in (observed.store.root / "evidence").glob("*.json"))
    assert KEY not in serialized and "TEST_FIXTURE_PRIVATE_REASONING" not in serialized


def test_versioned_policy_retains_explicit_legacy_form():
    current = policy_manifest(INTELLIGENCE_PROFILE)
    legacy = policy_manifest(INTELLIGENCE_PROFILE, response_observation_contract="")
    assert current["model_contract"]["response_observation_contract"] == CONTRACT
    assert "response_observation_contract" not in legacy["model_contract"]
    removed = deepcopy(current); removed["model_contract"].pop("response_observation_contract")
    assert removed == legacy


def test_new_frozen_source_rejects_coherently_rehashed_missing_contract(tmp_path, monkeypatch):
    from test_agent_runtime_policy import prepared
    from trading_system import agent_runtime as runtime
    from trading_system.agent_evidence import atomic_json, digest, json_bytes, EvidenceInvalid
    for name in ("TIGER_INTEL_REGISTRY_FILE", "TIGER_INTEL_ARCHIVE_ROOT", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    _state, run, admission, policy, _source = prepared(tmp_path, monkeypatch)
    assert runtime.validate_admission(run)[1]["model_contract"]["response_observation_contract"] == CONTRACT
    policy["model_contract"].pop("response_observation_contract")
    atomic_json(run, "policy.json", policy)
    admission["policy_sha256"] = digest(json_bytes(policy)); atomic_json(run, "admission.json", admission)
    with pytest.raises(EvidenceInvalid, match="Admission policy differs"):
        runtime.validate_admission(run)


@pytest.mark.parametrize("value", ["*", "api.deepseek.com", "localhost,127.0.0.1,::1,evil.example",
    "localhost,127.0.0.1,::1,https://api.deepseek.com", "localhost,127.0.0.1,::1,api.deepseek.com@evil.example",
    "localhost,127.0.0.1,::1,api.deepseek.com,api.deepseek.com", ""])
def test_trusted_proxy_bypass_rejects_unlisted_or_ambiguous_values(value):
    with pytest.raises(ValueError): trusted_model_no_proxy(value)


def test_fixed_model_bypass_preserves_other_proxy_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    env = run_environment(tmp_path, model_required=True)
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1,api.deepseek.com"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    monkeypatch.delenv("NO_PROXY")
    assert run_environment(tmp_path, model_required=True)["NO_PROXY"] == "localhost,127.0.0.1,::1"


@pytest.mark.parametrize("change", [
    {"schema": []}, {"json_stage": []}, {"json_stage": "unknown"}, {"json_stage": None},
    {"content": "TEST_FIXTURE_private"}, {"transport": {"headers": "TEST_FIXTURE_private"}},
])
def test_versioned_failure_diagnostic_remains_closed_and_value_free(change):
    value = {"schema": "tiger.model-failure.v2", "provider": "deepseek",
             "code": "INVALID_RESPONSE_JSON", "json_stage": "http_envelope", **change}
    with pytest.raises(ValueError) as error:
        validate_model_diagnostic(value)
    assert "TEST_FIXTURE_private" not in str(error.value)
