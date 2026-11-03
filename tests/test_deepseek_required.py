"""Required DeepSeek transport contracts; all HTTP is deterministic and mocked."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import requests

from trading_system.llm.deepseek_required import (
    DeepSeekRequiredClient, sanitize_url, validate_schema,
)
from trading_system.redline import LLMUnavailable
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


KEY = "fixture-deepseek-credential"
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"score": {"type": "number", "minimum": 0, "maximum": 10},
                   "reason": {"type": "string", "minLength": 1}},
    "required": ["score", "reason"],
}


class RecordingExecution:
    def __init__(self, stage="clean.llm_semantic"):
        self._active = SimpleNamespace(spec=SimpleNamespace(name=stage))
        self.calls = []

    def call_io(self, method, inputs, fn):
        call = {"method": method, "inputs": copy.deepcopy(inputs)}
        self.calls.append(call)
        result = fn()
        call["output"] = copy.deepcopy(result)
        return result


class Response:
    def __init__(self, payload=None, *, body=None, status=200, headers=None):
        self.body = body if body is not None else json.dumps(payload).encode()
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.raw = io.BytesIO(self.body)
        self.closed = False

    def close(self):
        self.closed = True


def chat_response(parsed=None, **overrides):
    payload = {
        "id": "deepseek-response-1", "model": "deepseek-flash",
        "system_fingerprint": "fp-fixture", "object": "chat.completion",
        "choices": [{"finish_reason": "stop", "index": 0,
                     "message": {"role": "assistant", "content": json.dumps(
                         parsed if parsed is not None else {"score": 7, "reason": "evidence"})}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50,
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def configured(monkeypatch):
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": KEY}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    return RecordingExecution()


def test_required_chat_binds_keyless_request_and_actual_response(configured, monkeypatch):
    seen = []
    response = Response(chat_response())

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return response

    monkeypatch.setattr(requests, "post", post)
    client = DeepSeekRequiredClient(configured)
    out = client.complete_json(system="Analyze JSON only", user="public evidence",
                               schema_hint={"score": 0}, schema=SCHEMA)
    assert out == {"score": 7, "reason": "evidence"}
    assert client.required is True
    url, kwargs = seen[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert kwargs["headers"]["Authorization"] == f"Bearer {KEY}"
    assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
    assert kwargs["stream"] is True
    body = json.loads(kwargs["data"])
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body
    assert body["stream"] is False
    call = configured.calls[0]
    assert call["method"] == "model.deepseek.chat"
    proof = call["output"]
    assert proof["schema"] == "tiger.deepseek-call.v1"
    assert proof["parsed"] == out
    assert proof["stage"] == "clean.llm_semantic"
    assert proof["response_id"] == "deepseek-response-1"
    assert proof["transport_response_sha256"] == hashlib.sha256(response.body).hexdigest()
    assert json.loads(proof["response"]["choices"][0]["message"]["content"]) == out
    assert proof["response_sha256"] == hashlib.sha256(json.dumps(
        proof["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    assert proof["schema_sha256"] == call["inputs"]["schema_sha256"]
    assert proof["request_sha256"] == call["inputs"]["request_sha256"]
    assert proof["usage"]["total_tokens"] == 50
    assert proof["scene"] == "news-labeling"
    assert proof["thinking_mode"] == "disabled"
    assert proof["reasoning_effort"] is None
    assert proof["input_token_estimate"] == call["inputs"]["input_token_estimate"]
    assert proof["output_ceiling"] == body["max_tokens"] > 16_384
    assert call["inputs"]["request"] == body
    assert client.receipts[0]["response_id"] == proof["response_id"]
    assert "parsed" not in client.receipts[0]
    assert "response" not in client.receipts[0]
    assert KEY not in json.dumps(configured.calls)
    assert KEY not in json.dumps(client.describe())
    assert client.describe()["remote_requests"] == 1
    assert client.describe()["thinking_mode"] == "scene-policy"
    assert client.describe()["budget"]["context_tokens"] == 1_048_576
    assert response.closed


def test_nonzero_reasoning_tokens_are_rejected_for_non_thinking_calls(configured, monkeypatch):
    payload = chat_response()
    payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 1}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(payload))
    with pytest.raises(LLMUnavailable, match="NON_THINKING_OUTPUT_REQUIRED"):
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=SCHEMA)


def test_legacy_max_tokens_argument_cannot_shrink_the_complete_window(configured, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(chat_response()))
    client = DeepSeekRequiredClient(configured)
    client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA, max_tokens=1)
    assert client.receipts[0]["output_ceiling"] > 300_000


def test_risk_scene_enables_high_thinking_and_accepts_reasoning_usage(configured, monkeypatch):
    configured._active.spec.name = "tech.risk"
    payload = chat_response()
    payload["choices"][0]["message"]["reasoning_content"] = "private reasoning is not persisted"
    payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 7}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(payload))
    client = DeepSeekRequiredClient(configured)
    out = client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert out == {"score": 7, "reason": "evidence"}
    body = configured.calls[0]["inputs"]["request"]
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body
    proof = configured.calls[0]["output"]
    assert proof["thinking_mode"] == "enabled"
    assert proof["reasoning_effort"] == "high"
    assert proof["usage"]["completion_tokens_details"]["reasoning_tokens"] == 7
    assert "private reasoning" not in json.dumps(configured.calls)


@pytest.mark.parametrize("name,value", [
    ("LLM_BACKEND", "auto"), ("LLM_BACKEND", "kimi"),
    ("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    ("LLM_BASE_URL", "http://api.deepseek.com"),
    ("LLM_BASE_URL", "https://api.deepseek.com@evil.example"),
    ("LLM_MODEL", "deepseek-chat"), ("LLM_MODEL", "deepseek-v4-pro"),
    ("LLM_API_KEY", ""), ("LLM_API_KEY", "unsafe\r\nheader"),
])
def test_trusted_configuration_fails_closed(configured, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(LLMUnavailable):
        DeepSeekRequiredClient(configured)


@pytest.mark.parametrize("status", [301, 400, 401, 402, 403, 408, 429, 500, 504])
def test_http_error_never_retries_or_leaks_body(configured, monkeypatch, status):
    seen = []
    response = Response(body=f"{KEY} accessToken=private-token".encode(), status=status)
    monkeypatch.setattr(requests, "post", lambda *a, **k: seen.append(1) or response)
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(LLMUnavailable) as error:
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert str(status) in str(error.value)
    assert KEY not in str(error.value) and "private-token" not in str(error.value)
    assert len(seen) == 1 and not client.receipts and response.closed


@pytest.mark.parametrize("change", [
    {"model": "deepseek-v4-pro"}, {"id": ""}, {"usage": None},
    {"object": "not-chat-completion"},
    {"id": "r" * 161},
    {"choices": [{"index": 1, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]},
    {"usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 51}},
    {"usage": {"prompt_tokens": True, "completion_tokens": 10, "total_tokens": 11}},
    {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
    {"choices": [{"finish_reason": "tool_calls", "message": {"content": "{}"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": [{}]}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "```json\n{}\n```"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"score":NaN,"reason":"x"}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"score":7,"score":8,"reason":"x"}'}}]},
])
def test_response_identity_completion_and_strict_json_required(configured, monkeypatch, change):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(chat_response(**change)))
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(LLMUnavailable):
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert not client.receipts


def test_network_exception_is_sanitized(configured, monkeypatch):
    def post(*args, **kwargs):
        raise requests.Timeout(f"{KEY} https://x.example?token=private-token")
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(LLMUnavailable) as error:
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert KEY not in str(error.value) and "private-token" not in str(error.value)


@pytest.mark.parametrize("headers,body", [
    ({"Content-Length": str(1024 * 1024 + 1)}, b"{}"),
    ({}, b" " * (1024 * 1024 + 1)),
    ({"Content-Encoding": "gzip"}, b"{}"),
    ({"Content-Type": "text/html"}, b"{}"),
])
def test_response_size_and_encoding_limits(configured, monkeypatch, headers, body):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(body=body, headers=headers))
    with pytest.raises(LLMUnavailable):
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=SCHEMA)


@pytest.mark.parametrize("url", [
    "https://example.com/?accessToken=private-token", "https://example.com/?ACCESS_TOKEN=x",
    "https://example.com/?%74oken=x", "https://example.com/?X-Amz-Signature=x",
    "https://user:pass@example.com/", "https://example.com/?foo=1&amp;api_key=x",
    "file:///etc/passwd", "http://localhost/", "http://127.0.0.1/", "http://2130706433/",
    "https://example.com/?%2561ccessToken=x", "https://exa%mple.com/",
    "https://example.com/?redirect=https%3A%2F%2Fother.example%2F%3Ftoken%3Dprivate",
])
def test_unsafe_urls_rejected(url):
    assert sanitize_url(url) is None


@pytest.mark.parametrize("layers", [4, 6, 8, 12])
def test_nested_percent_encoding_cannot_hide_credentials(layers):
    key = "%74oken"
    encoded_secret = "".join("%%%02X" % ord(char) for char in KEY)
    for _ in range(layers):
        key = quote(key, safe="")
        encoded_secret = quote(encoded_secret, safe="")
    assert sanitize_url("https://example.com/?" + key + "=private-value") is None
    assert sanitize_url("https://example.com/" + encoded_secret, secret=KEY) is None


def test_native_search_sanitizes_before_evidence_and_model(configured, monkeypatch):
    configured._active.spec.name = "search.collect"
    unsafe = "https://example.com/doc?accessToken=private-jwt"
    body = {"results": [
        {"url": unsafe, "title": "bad", "content": "do not persist"},
        {"url": "https://example.com/release#fragment", "title": f"Public {KEY}",
         "content": f"Evidence {unsafe} second https://user:pass@evil.example/ {KEY}",
         "date": "2026-10-25T00:00:00Z"},
    ]}
    response = Response(body)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    client = DeepSeekRequiredClient(configured)
    result = client.search("public evidence", count=4, need_content=True)
    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/release"
    assert result[0]["published"] == "2026-10-25T00:00:00Z"
    proof = configured.calls[0]["output"]
    assert proof["schema"] == "tiger.deepseek-search.v1"
    assert proof["result_count"] == 2 and proof["usable_result_count"] == 1
    assert proof["results"] == result
    assert proof["transport_response_sha256"] == hashlib.sha256(response.body).hexdigest()
    assert proof["response_sha256"] == hashlib.sha256(json.dumps(
        {"results": result}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    persisted = json.dumps(configured.calls)
    assert KEY not in persisted and "private-jwt" not in persisted
    assert "user:pass" not in persisted and "#fragment" not in persisted
    assert client.search_receipts[0]["usable_result_count"] == 1
    assert "results" not in client.search_receipts[0]


def test_chat_redacts_key_and_credential_links_before_persistence(configured, monkeypatch):
    dangerous = f"source https://example.com/?token=private-jwt {KEY}"
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(chat_response(
        {"score": 7, "reason": dangerous})))
    client = DeepSeekRequiredClient(configured)
    output = client.complete_json(system=f"JSON {KEY}", user=dangerous,
                                  schema_hint={}, schema=SCHEMA)
    serialized = json.dumps([output, configured.calls])
    assert KEY not in serialized and "private-jwt" not in serialized


def test_required_chat_retries_one_schema_mismatch_per_run(configured, monkeypatch):
    first_invalid = chat_response({"score": 12, "reason": KEY}, id="invalid-first")
    valid = chat_response(id="valid-second")
    next_invalid = chat_response({"score": 12, "reason": "another"}, id="invalid-third")
    responses = iter([first_invalid, valid, next_invalid])
    sent = []

    def post(url, **kwargs):
        sent.append((url, kwargs["data"]))
        return Response(next(responses))

    monkeypatch.setattr(requests, "post", post)
    client = DeepSeekRequiredClient(configured)
    assert client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)["score"] == 7
    assert sent[0] == sent[1]
    proof = configured.calls[0]["output"]
    assert proof["response_id"] == "valid-second"
    assert proof["retry_diagnostics"] == [{
        "schema": "tiger.model-failure.v1", "provider": "deepseek",
        "code": "OUTPUT_SCHEMA_MISMATCH", "schema_path": "/score", "schema_keyword": "maximum",
    }]
    assert KEY not in json.dumps(configured.calls)
    assert "invalid-first" not in json.dumps(configured.calls)
    with pytest.raises(LLMUnavailable, match="OUTPUT_SCHEMA_MISMATCH"):
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert len(sent) == 3
    assert len(client.receipts) == 1


@pytest.mark.parametrize("payload", [{}, {"results": [
    {"url": "https://example.com/?token=x", "title": "unsafe", "content": "body"}]}])
def test_search_without_usable_evidence_fails(configured, monkeypatch, payload):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(payload))
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(LLMUnavailable):
        client.search("public evidence")
    assert not client.search_receipts


def test_native_search_records_zero_hit_topic(configured, monkeypatch):
    configured._active.spec.name = "search.collect"
    response = Response({"results": []})
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    client = DeepSeekRequiredClient(configured)
    assert client.search("a narrowly defined public topic") == []
    proof = configured.calls[0]["output"]
    assert proof["result_count"] == proof["usable_result_count"] == 0
    assert len(client.search_receipts) == 1


@pytest.mark.parametrize("first_status", [408, 429, 503])
def test_native_search_retries_transient_http_once_with_keyless_proof(
        configured, monkeypatch, first_status):
    configured._active.spec.name = "search.collect"
    transient = Response({"error": KEY}, status=first_status)
    valid = Response({"results": [{
        "url": "https://example.com/real-document", "title": "Public evidence",
        "content": "A factual observation from the original source.",
    }]})
    pending = iter([transient, valid])
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(pending))
    monkeypatch.setattr("trading_system.llm.deepseek_required.time.sleep", lambda _: None)
    client = DeepSeekRequiredClient(configured)
    assert len(client.search("public evidence")) == 1
    proof = configured.calls[0]["output"]
    assert [item["http_status"] for item in proof["retry_diagnostics"]] == [first_status]
    assert proof["transport_response_sha256"] == hashlib.sha256(valid.body).hexdigest()
    assert transient.closed and valid.closed
    assert client.describe()["remote_requests"] == 2
    assert KEY not in json.dumps(configured.calls)


def test_native_search_never_retries_auth_failure_or_exhausted_retry(
        configured, monkeypatch):
    configured._active.spec.name = "search.collect"
    responses = []
    def post(*a, **k):
        response = Response(status=status)
        responses.append(response)
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("trading_system.llm.deepseek_required.time.sleep", lambda _: None)
    status = 401
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(LLMUnavailable, match="HTTP_401"):
        client.search("public evidence")
    assert len(responses) == 1
    status = 503
    with pytest.raises(LLMUnavailable, match="HTTP_503"):
        client.search("public evidence")
    assert len(responses) == 3
    assert all(response.closed for response in responses)
    assert not client.search_receipts


def test_fetch_validates_url_and_records_sanitized_page(configured, monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: seen.append((url, kw)) or Response(
        {"title": "Original", "content": "Verified public page"}))
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(LLMUnavailable):
        client.fetch("https://example.com/?token=private")
    assert not seen
    result = client.fetch("https://example.com/article#part")
    assert seen[0][0] == "https://api.deepseek.com/v1/tools/fetch"
    assert result["url"] == "https://example.com/article"
    assert configured.calls[0]["method"] == "model.deepseek.fetch"


@pytest.mark.parametrize("value", [
    {"score": True, "reason": "bad"}, {"score": float("nan"), "reason": "bad"},
    {"score": 11, "reason": "bad"}, {"score": 1, "reason": ""},
    {"score": 1}, {"score": 1, "reason": "ok", "extra": 1},
])
def test_closed_schema_rejects_bad_values(value):
    with pytest.raises(LLMUnavailable):
        validate_schema(value, SCHEMA)


def test_schema_combinators_and_arrays():
    schema = {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
              "items": {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]}}
    validate_schema([1, None], schema)
    for value in [[1, 1], [], [0], [True], [1, None, 2]]:
        with pytest.raises(LLMUnavailable):
            validate_schema(value, schema)


def test_unknown_schema_keyword_rejected_before_http(configured):
    with pytest.raises(LLMUnavailable):
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema={"type": "object", "unknown": True})


def test_optional_usage_detail_fields_are_not_fabricated(configured, monkeypatch):
    payload = chat_response()
    payload["usage"]["prompt_tokens_details"] = {}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(payload))
    client = DeepSeekRequiredClient(configured)
    client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert "prompt_tokens_details" not in client.receipts[0]["usage"]


def test_requires_active_owned_execution(configured):
    with pytest.raises(LLMUnavailable):
        DeepSeekRequiredClient(None)
    client = DeepSeekRequiredClient(configured)
    configured._active = None
    with pytest.raises(LLMUnavailable):
        client.search("evidence")


def test_close_failure_and_deadline_are_safe(configured, monkeypatch):
    response = Response(chat_response())
    def bad_close():
        raise OSError(KEY)
    response.close = bad_close
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(LLMUnavailable, match="RESPONSE_CLOSE_FAILURE") as error:
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert KEY not in str(error.value)
    import trading_system.llm.deepseek_required as transport
    moments = iter([0.0, 181.0])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(moments))
    response = Response(chat_response())
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(LLMUnavailable, match="RESPONSE_DEADLINE"):
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert response.closed


def test_tainted_schema_and_deep_model_json_fail_without_secret_leak(configured, monkeypatch):
    unsafe = copy.deepcopy(SCHEMA)
    unsafe["properties"]["reason"]["enum"] = [KEY]
    with pytest.raises(LLMUnavailable, match="UNSAFE_OUTPUT_SCHEMA"):
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=unsafe)
    nested = "leaf"
    for _ in range(70):
        nested = [nested]
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(chat_response(
        {"score": 1, "reason": nested})))
    with pytest.raises(LLMUnavailable, match="RESPONSE_DEPTH_LIMIT"):
        DeepSeekRequiredClient(configured).complete_json(
            system="s", user="u", schema_hint={}, schema=SCHEMA)


def test_evidence_failure_is_preserved_before_network(configured):
    from trading_system.redline.execution import EvidenceWriteError
    original = EvidenceWriteError("fixture evidence store failed")
    def reject(*args, **kwargs):
        raise original
    configured.call_io = reject
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(EvidenceWriteError) as error:
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert error.value is original
    assert client.describe()["remote_requests"] == 0


def test_search_publisher_limits_reject_without_truncation(configured, monkeypatch):
    long_url = "https://example.com/" + "a" * 2050
    assert sanitize_url(long_url) is None
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response({"results": [
        {"url": "https://example.com/", "title": "a" * 501, "content": "public evidence"}]}))
    with pytest.raises(LLMUnavailable, match="NO_USABLE_SEARCH_RESULTS"):
        DeepSeekRequiredClient(configured).search("public evidence")


@pytest.mark.parametrize("method", ["search", "fetch"])
def test_source_title_is_formatted_before_identity_and_proof(configured, monkeypatch, method):
    title = "\t<SEC>\r\n Q3\x00\x7f\x85  Report  "
    page = {"url": "https://example.com/report", "title": title,
            "content": "Original public report content"}
    payload = {"results": [page]} if method == "search" else page
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(payload))
    client = DeepSeekRequiredClient(configured)
    if method == "search":
        result = client.search("public report")[0]
        proof_result = configured.calls[0]["output"]["results"][0]
    else:
        result = client.fetch(page["url"])
        proof_result = configured.calls[0]["output"]["page"]
    assert result["title"] == "SEC Q3 Report"
    assert proof_result == result
    identity = {key: result[key] for key in ("url", "title", "content", "published")}
    assert result["id"] == "ds_" + hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()[:24]


@pytest.mark.parametrize("title", ["", " <>\x00\t\x7f ", "A" * 501])
@pytest.mark.parametrize("method", ["search", "fetch"])
def test_missing_or_oversize_source_title_is_never_invented_or_truncated(
        configured, monkeypatch, title, method):
    page = {"url": "https://example.com/report", "title": title,
            "content": "Original public report content"}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(
        {"results": [page]} if method == "search" else page))
    client = DeepSeekRequiredClient(configured)
    expected = "NO_USABLE_SEARCH_RESULTS" if method == "search" else "SOURCE_TITLE_"
    with pytest.raises(LLMUnavailable, match=expected):
        client.search("public report") if method == "search" else client.fetch(page["url"])
    assert not client.search_receipts and not client.fetch_receipts


@pytest.mark.parametrize("value,schema,path,keyword", [
    ("secret-modeltext", {"type": "number"}, "", "type"),
    (1, {"const": 2}, "", "const"),
    (1, {"enum": [2, 3]}, "", "enum"),
    (1, {"allOf": [{"type": "number"}, {"minimum": 2}]}, "", "allOf"),
    (1, {"anyOf": [{"type": "string"}, {"type": "null"}]}, "", "anyOf"),
    (1, {"oneOf": [{"type": "number"}, {"type": "integer"}]}, "", "oneOf"),
    ({}, {"type": "object", "required": ["declared"]}, "", "required"),
    ({KEY: "secret-modeltext"}, {"type": "object", "additionalProperties": False}, "", "additionalProperties"),
    ({KEY: "secret-modeltext"}, {"type": "object", "additionalProperties": {"type": "number"}}, "", "additionalProperties"),
    ([], {"type": "array", "minItems": 1}, "", "minItems"),
    ([1, 2], {"type": "array", "maxItems": 1}, "", "maxItems"),
    ([1, 1.0], {"type": "array", "uniqueItems": True}, "", "uniqueItems"),
    ([1, "secret-modeltext"], {"type": "array", "items": {"type": "number"}}, "/1", "type"),
    ("", {"type": "string", "minLength": 1}, "", "minLength"),
    ("secret-modeltext", {"type": "string", "maxLength": 3}, "", "maxLength"),
    ("secret-modeltext", {"type": "string", "pattern": "^public$"}, "", "pattern"),
    (0, {"type": "number", "minimum": 1}, "", "minimum"),
    (2, {"type": "number", "maximum": 1}, "", "maximum"),
    (1, {"type": "number", "exclusiveMinimum": 1}, "", "exclusiveMinimum"),
    (1, {"type": "number", "exclusiveMaximum": 1}, "", "exclusiveMaximum"),
])
def test_schema_failure_diagnostic_has_only_controlled_location(value, schema, path, keyword):
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema(value, schema)
    assert error.value.diagnostic == {
        "schema": "tiger.model-failure.v1", "provider": "deepseek",
        "code": "OUTPUT_SCHEMA_MISMATCH", "schema_path": path, "schema_keyword": keyword,
    }
    assert KEY not in json.dumps(error.value.diagnostic)
    assert "secret-modeltext" not in json.dumps(error.value.diagnostic)


def test_schema_diagnostic_nested_path_masks_unsafe_schema_names():
    schema = {"type": "object", "properties": {"chains": {"type": "array", "items": {
        "type": "object", "properties": {"heat": {"type": "number", "maximum": 10}},
        "additionalProperties": False}}}}
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema({"chains": [{"heat": 11}]}, schema)
    assert error.value.diagnostic["schema_path"] == "/chains/0/heat"
    assert error.value.diagnostic["schema_keyword"] == "maximum"
    unsafe = "unsafe/schema-key"
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema({unsafe: "secret-modeltext"}, {"type": "object", "properties": {
            unsafe: {"type": "number"}}})
    assert error.value.diagnostic["schema_path"] == "/*"
    assert unsafe not in json.dumps(error.value.diagnostic)


def test_schema_diagnostic_path_is_bounded_without_partial_property_names():
    name = "a" * 127
    value, schema = "secret-modeltext", {"type": "number"}
    for _ in range(6):
        value, schema = {name: value}, {"type": "object", "properties": {name: schema}}
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema(value, schema)
    diagnostic = error.value.diagnostic
    assert len(diagnostic["schema_path"]) <= 512
    assert all(component == name for component in diagnostic["schema_path"].split("/")[1:])
    assert diagnostic["schema_keyword"] == "depth"


@pytest.mark.parametrize("failure,code,status", [
    ("http", "HTTP_ERROR", 401), ("timeout", "NETWORK_TIMEOUT", None),
    ("network", "TRANSPORT_FAILURE", None), ("json", "INVALID_RESPONSE_JSON", None),
])
def test_transport_failure_diagnostic_never_contains_body_headers_or_exception_text(
        configured, monkeypatch, failure, code, status):
    tainted = f"{KEY} secret-modeltext https://example.com/?token=private-token"
    def post(*args, **kwargs):
        if failure == "timeout":
            raise requests.Timeout(tainted)
        if failure == "network":
            raise OSError(tainted)
        return Response(body=tainted.encode(), status=401 if failure == "http" else 200)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RequiredModelUnavailable) as error:
        DeepSeekRequiredClient(configured).complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    expected = {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": code}
    if status is not None:
        expected["http_status"] = status
        assert str(error.value) == "DeepSeek required: HTTP_401"
    assert error.value.diagnostic == expected
    persisted = json.dumps([configured.calls, error.value.diagnostic, str(error.value)])
    assert KEY not in persisted and "secret-modeltext" not in persisted and "private-token" not in persisted


def test_model_schema_failure_keeps_no_reasoning_content_or_unknown_key(configured, monkeypatch):
    configured._active.spec.name = "tech.risk"
    payload = chat_response({"score": 7, "reason": "public", KEY: "secret-modeltext"})
    payload["choices"][0]["message"]["reasoning_content"] = KEY + " secret-reasoning"
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(payload))
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(RequiredModelUnavailable) as error:
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA)
    assert error.value.diagnostic["schema_path"] == ""
    assert error.value.diagnostic["schema_keyword"] == "additionalProperties"
    serialized = json.dumps([configured.calls, error.value.diagnostic, str(error.value)])
    assert KEY not in serialized and "secret-modeltext" not in serialized and "secret-reasoning" not in serialized
    assert not client.receipts


def test_actual_key_in_schema_name_fails_before_schema_location_is_reported(configured):
    schema = {"type": "object", "properties": {KEY: {"type": "number"}}}
    with pytest.raises(RequiredModelUnavailable) as error:
        DeepSeekRequiredClient(configured).complete_json(system="s", user="u", schema_hint={}, schema=schema)
    assert error.value.diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek",
                                      "code": "UNSAFE_OUTPUT_SCHEMA"}
    assert KEY not in json.dumps(error.value.diagnostic)
    assert configured.calls == []
