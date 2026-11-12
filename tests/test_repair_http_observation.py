"""EP03 real transport boundary regression; all provider replies are TEST_FIXTURE."""
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
import requests

from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.json_observation import CONTRACT, validate_transport
from trading_system.intelligence.transport import HttpTransport
from trading_system.intelligence.contracts import SourceError
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


class Response:
    def __init__(self, body, status=400, headers=None):
        self.raw = io.BytesIO(body)
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.closed = False
    def close(self):
        self.closed = True


def model_client(monkeypatch):
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "TEST_FIXTURE_SECRET"}.items():
        monkeypatch.setenv(key, value)
    execution = SimpleNamespace(call_io=lambda method, inputs, fn: fn(),
        _active=SimpleNamespace(spec=SimpleNamespace(name="clean.llm_semantic")),
        response_observation_contract=CONTRACT, model_attempt_snapshot=lambda x: x,
        claim_model_format_retry=lambda: False)
    return DeepSeekRequiredClient(execution)


@pytest.mark.parametrize("status", [400, 401, 402, 422, 429, 503])
def test_model_http_failure_records_body_without_secrets(monkeypatch, status):
    body = json.dumps({"error": {"message": "TEST_FIXTURE_SECRET max_tokens exceeds context window", "code": "invalid_request_error"}}).encode()
    response = Response(body, status)
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or response)
    client = model_client(monkeypatch)
    with pytest.raises(RequiredModelUnavailable) as caught:
        client._post_json("/chat/completions", {"TEST_FIXTURE": True})
    assert caught.value.diagnostic["http_status"] == status
    transport = validate_transport(client._last_chat_transport)
    assert transport["body_bytes"] == len(body)
    assert transport["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert transport["body_complete"] is True
    error = transport["http_error"]
    assert error["schema"] == "tiger.http-error-observation/v1"
    assert error["body_scope"] == "complete_http_body"
    assert error["usage_state"] == "unknown"
    assert "TEST_FIXTURE_SECRET" not in json.dumps(transport)
    assert len(calls) == 1 and response.closed


def test_model_error_body_is_bounded_and_never_complete(monkeypatch):
    body = b"TEST_FIXTURE" * 100000
    response = Response(body)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    client = model_client(monkeypatch)
    with pytest.raises(RequiredModelUnavailable):
        client._post_json("/chat/completions", {"TEST_FIXTURE": True})
    transport = validate_transport(client._last_chat_transport)
    assert 0 < transport["body_bytes"] <= 65536
    assert response.raw.tell() == transport["body_bytes"]
    assert transport["body_complete"] is False
    assert transport["body_sha256"] == hashlib.sha256(body[:transport["body_bytes"]]).hexdigest()
    assert transport["http_error"]["body_scope"] == "observed_prefix"
    assert transport["http_error"]["stop_reason"] == "byte_limit"


def test_source_http_failure_accounts_observed_bytes(monkeypatch):
    body = b'{"error":"TEST_FIXTURE token=SECRET"}'
    response = Response(body, 400)
    session = SimpleNamespace(request=lambda *a, **k: response)
    transport = HttpTransport(session=session, allow_local=True)
    with pytest.raises(SourceError) as caught:
        transport.request("GET", "http://127.0.0.1/fixture")
    failure = caught.value
    assert failure.http_status == 400 and failure.delivery == "returned"
    assert failure.bytes_received == len(body)
    error = failure.details["http_error"]
    assert error["body_complete"] and error["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert "SECRET" not in json.dumps(failure.details)
    assert transport.total_bytes_received == len(body) and transport.total_request_count == 1


@pytest.mark.parametrize("headers,body,stop,complete", [
    ({"Content-Length": "4"}, b"ab", "incomplete_body", False),
    ({"Content-Length": "2"}, b"ab", "content_length", True),
    ({"Content-Length": "bad"}, b"ab", "invalid_length", False),
    ({"Content-Encoding": "gzip"}, b"ab", "encoding_denied", False),
    ({}, b"", "eof", True),
])
def test_error_observation_has_explicit_incomplete_cases(headers, body, stop, complete):
    import time
    from trading_system.llm.http_observation import observe_http_error
    result = observe_http_error(Response(body, headers=headers), status=400,
                                deadline=time.monotonic() + 1)
    assert result["stop_reason"] == stop and result["body_complete"] is complete
    assert result["usage_state"] == "unknown"


def test_error_observation_does_not_overrun_deadline_or_reset_read_failure():
    import time
    from trading_system.llm.http_observation import observe_http_error
    response = Response(b"TEST_FIXTURE")
    result = observe_http_error(response, status=400, deadline=time.monotonic() - 1)
    assert result["stop_reason"] == "deadline" and response.raw.tell() == 0
    class Broken:
        def read1(self, size):
            raise OSError("SECRET exception body")
    response.raw = Broken()
    result = observe_http_error(response, status=400, deadline=time.monotonic() + 1)
    assert result["stop_reason"] == "read_failure" and "SECRET" not in json.dumps(result)


def test_error_observation_cannot_be_attached_to_success_or_lie_about_hash():
    import time
    from trading_system.llm.http_observation import observe_http_error
    error = observe_http_error(Response(b"test"), status=400, deadline=time.monotonic() + 1)
    transport = {k: error[k] for k in ["http_status", "body_sha256", "body_bytes", "body_whitespace_only", "body_complete"]}
    transport["http_error"] = error
    validate_transport(transport)
    for key, value in [("http_status", 200), ("body_sha256", "0" * 64), ("body_complete", False)]:
        with pytest.raises(ValueError):
            validate_transport({**transport, key: value})
