"""A rejected, known provider response is charged once but never becomes success."""
from copy import deepcopy
import json

import pytest
import requests

from test_deepseek_json_observation import observed, native, invoke, snapshot
from test_deepseek_required import Response
from trading_system.llm.json_observation import replay_attempt
from trading_system.llm.rejected_native import CONTRACT
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


@pytest.mark.parametrize("change,code", [
    ("length", "MODEL_OUTPUT_TRUNCATED"),
    ("tool_calls", "INCOMPLETE_MODEL_RESPONSE"),
    ("bad_index", "INCOMPLETE_MODEL_RESPONSE"),
    ("choices", "INVALID_CHOICES"),
])
def test_known_rejected_native_keeps_usage_without_retry_or_success(observed, monkeypatch, change, code):
    observed.rejected_native_contract = CONTRACT
    response = native('{"score":7', "rejected-" + change)
    if change in {"length", "tool_calls"}:
        response["choices"][0]["finish_reason"] = change
    elif change == "bad_index":
        response["choices"][0]["index"] = 7
    else:
        response["choices"] = []
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(k["data"]) or Response(response))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"TEST_FIXTURE": change}):
            invoke(observed)
    assert len(sent) == 1 and error.value.diagnostic["code"] == code
    entry = error.value.observation["attempts"][0]
    attempt = snapshot(observed, entry["observation_ref"])
    rejected = attempt["rejected_native"]
    assert rejected["response_id"] == response["id"] and rejected["usage"] == response["usage"]
    if change in {"length", "tool_calls"}:
        assert rejected["finish_reason"] == change
    events = [json.loads(line) for line in (observed.store.root / "events.jsonl").read_text().splitlines()]
    from trading_system.agent_evidence import read_snapshot
    stage = read_snapshot(observed.store.root, events[-1]["output_ref"], run_id=observed.run_id,
                          stage="clean.llm_semantic", role="output")
    request = read_snapshot(observed.store.root, stage["io_calls"][0]["input_ref"],
                           run_id=observed.run_id, stage="clean.llm_semantic", role="input")
    verdict = replay_attempt(attempt, request)
    assert verdict["usage"] == response["usage"] and verdict["parsed"] is None
    assert verdict["retry_eligible"] is False and verdict["diagnostic"] == entry["diagnostic"]
    old_request = {key: value for key, value in request.items() if key != "rejected_native_contract"}
    with pytest.raises(ValueError):
        replay_attempt(attempt, old_request)


def test_old_unknown_failure_is_not_upgraded_and_unknown_usage_stays_unknown(observed, monkeypatch):
    response = native("", "missing-usage")
    response["choices"][0]["finish_reason"] = "length"
    response.pop("usage")
    observed.rejected_native_contract = CONTRACT
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(response))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "unknown"}):
            invoke(observed)
    attempt = snapshot(observed, error.value.observation["attempts"][0]["observation_ref"])
    assert attempt["response"] is None and "rejected_native" not in attempt


def test_rejected_native_does_not_retain_provider_content_or_secret_text(observed, monkeypatch):
    from test_deepseek_required import KEY
    observed.rejected_native_contract = CONTRACT
    response = native("Credential " + KEY, "private-content-rejected")
    response["choices"][0]["finish_reason"] = "length"
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(response))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step("clean.llm_semantic", {"TEST_FIXTURE": "redaction"}):
            invoke(observed)
    attempt = snapshot(observed, error.value.observation["attempts"][0]["observation_ref"])
    assert KEY not in json.dumps(attempt) and "Credential" not in json.dumps(attempt)
    assert attempt["rejected_native"]["content_bytes"] == len(("Credential " + KEY).encode())


def test_known_rejected_v3_cannot_accept_coherently_rehashed_wrong_route(observed, monkeypatch):
    from trading_system.agent_evidence import read_snapshot
    from trading_system.llm.json_observation import TOOL_CONTRACT
    observed.response_observation_contract = TOOL_CONTRACT
    observed.rejected_native_contract = CONTRACT
    response = native('', 'route-rejected')
    response['choices'][0]['finish_reason'] = 'length'
    def post(url, **kwargs):
        result = Response(response)
        result.url, result.history = url, []
        return result
    monkeypatch.setattr(requests, 'post', post)
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step('clean.llm_semantic', {'TEST_FIXTURE': 'route'}):
            invoke(observed)
    attempt = snapshot(observed, error.value.observation['attempts'][0]['observation_ref'])
    events = [json.loads(line) for line in (observed.store.root / 'events.jsonl').read_text().splitlines()]
    stage = read_snapshot(observed.store.root, events[-1]['output_ref'], run_id=observed.run_id,
                          stage='clean.llm_semantic', role='output')
    request = read_snapshot(observed.store.root, stage['io_calls'][0]['input_ref'],
                            run_id=observed.run_id, stage='clean.llm_semantic', role='input')
    assert replay_attempt(attempt, request)['usage']['total_tokens'] == 50
    for route, redirected in [('https://api.deepseek.com/beta/chat/completions', False),
                              (request['endpoint'], True)]:
        tampered = deepcopy(attempt)
        tampered['transport'].update(final_response_url=route, redirected=redirected)
        tampered['transport_diagnostic']['transport'] = deepcopy(tampered['transport'])
        with pytest.raises(ValueError, match='route differs'):
            replay_attempt(tampered, request)


@pytest.mark.parametrize('encoding', ['literal', 'unicode', 'percent', 'base64'])
def test_rejected_response_identity_cannot_persist_encoded_credentials(observed, monkeypatch, encoding):
    import base64
    from urllib.parse import quote
    from test_deepseek_required import KEY
    value = {'literal': KEY, 'unicode': ''.join('\\u%04x' % ord(c) for c in KEY),
             'percent': ''.join('%%%02X' % ord(c) for c in KEY),
             'base64': base64.b64encode(KEY.encode()).decode()}[encoding]
    observed.rejected_native_contract = CONTRACT
    response = native('', value)
    response['choices'][0]['finish_reason'] = 'length'
    monkeypatch.setattr(requests, 'post', lambda *a, **k: Response(response))
    with pytest.raises(RequiredModelUnavailable) as error:
        with observed.step('clean.llm_semantic', {'TEST_FIXTURE': encoding}):
            invoke(observed)
    attempt = snapshot(observed, error.value.observation['attempts'][0]['observation_ref'])
    assert attempt['response'] is None and 'rejected_native' not in attempt
    serialized = '\n'.join(p.read_text() for p in (observed.store.root / 'evidence').glob('*.json'))
    assert KEY not in serialized and value not in serialized
