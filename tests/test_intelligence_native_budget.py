"""The acquisition envelope bounds native tools without capping semantic chat."""
import json
import time

import pytest
import requests

from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.redline import LLMUnavailable
from test_deepseek_required import configured, Response, chat_response, SCHEMA


def response(payload):
    body = json.dumps(payload).encode()
    return Response(body=body, headers={"Content-Length": str(len(body))})


def test_native_wire_bytes_and_request_count_are_shared_with_source_receipt(configured, monkeypatch):
    native = response({"results": []})
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: native)
    client = DeepSeekRequiredClient(configured)
    with client.native_io_budget(max_bytes=1024, deadline=time.monotonic() + 5) as meter:
        assert client.search("fixture", count=1) == []
    assert meter["transport_observed"] and meter["request_count"] == 1
    assert meter["bytes_received"] == len(native.body)
    assert client.search_receipts[0]["transport_usage"] == {
        "request_count": 1, "bytes_received": len(native.body)}
    assert client._native_io_limits is None


def test_expired_native_deadline_prevents_outbound_request(configured, monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: seen.append(True))
    client = DeepSeekRequiredClient(configured)
    with client.native_io_budget(max_bytes=1024, deadline=time.monotonic() - 1) as meter:
        with pytest.raises(LLMUnavailable, match="RESPONSE_DEADLINE"):
            client.search("fixture", count=1)
    assert meter["limit_reason"] == "deadline" and meter["request_count"] == 0 and seen == []


def test_native_body_cannot_be_read_past_remaining_aggregate_bytes(configured, monkeypatch):
    native = Response(body=b'{"results":[]}' + b" " * 1000)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: native)
    client = DeepSeekRequiredClient(configured)
    with client.native_io_budget(max_bytes=17, deadline=time.monotonic() + 5) as meter:
        with pytest.raises(LLMUnavailable, match="RESPONSE_TOO_LARGE"):
            client.search("fixture", count=1)
    assert meter["bytes_received"] == 17 and native.raw.tell() == 17
    assert meter["limit_reason"] == "bytes" and native.closed


def test_retry_uses_the_same_total_budget_and_counts_both_http_calls(configured, monkeypatch):
    native = response({"results": []})
    outcomes = [Response({"error": "retry fixture"}, status=503), native]
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: outcomes.pop(0))
    monkeypatch.setattr("trading_system.llm.deepseek_required.time.sleep", lambda seconds: None)
    client = DeepSeekRequiredClient(configured)
    with client.native_io_budget(max_bytes=1024, deadline=time.monotonic() + 5) as meter:
        assert client.search("fixture", count=1) == []
    assert meter["request_count"] == 2 and meter["bytes_received"] == len(native.body)
    assert client.search_receipts[0]["transport_usage"]["request_count"] == 2


def test_native_transport_envelope_never_changes_chat_legal_window(configured, monkeypatch):
    captured = []
    monkeypatch.setattr(requests, "post", lambda url, **kwargs:
        captured.append(json.loads(kwargs["data"])) or response(chat_response()))
    client = DeepSeekRequiredClient(configured)
    with client.native_io_budget(max_bytes=1, deadline=time.monotonic() - 1) as meter:
        output = client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert output["score"] == 7 and captured[0]["max_tokens"] == 393216
    assert meter["request_count"] == 0 and not meter["transport_observed"]


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_native_budget_is_trusted_typed_configuration(configured, maximum):
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(ValueError):
        with client.native_io_budget(max_bytes=maximum, deadline=time.monotonic() + 10):
            pytest.fail("Invalid context must not be entered")
