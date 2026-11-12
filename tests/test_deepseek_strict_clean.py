"""TEST_FIXTURE v3 real loopback HTTP, native evidence and independent replay.

The adapter maps the connection pool to loopback without changing the prepared
request URL or Response.url. This is explicitly an offline transport fixture,
never a real provider, TLS, financial-quality or business-production claim.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from threading import Thread

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool

from test_deepseek_required import KEY
from test_intelligence_json_observation_proof import make_execution, advance_to
from trading_system.agent_evidence import (EvidenceInvalid, atomic_json, json_bytes,
                                         read_json, read_snapshot)
from trading_system.llm.clean_keyed_codec import CONTRACT as CLEAN_CONTRACT
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.intelligence_proof import (verify_partial_intelligence_model,
                                                 verify_intelligence_model)
from trading_system.llm.json_observation import TOOL_CONTRACT, replay_attempt
from trading_system.llm.model_budget import CLEAN_TOOL_FUNCTION, count_json_tokens
from trading_system.llm.required_schemas import clean_schema
from trading_system.redline.execution import EvidenceWriteError, RequiredModelFailed
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


IDS = ["TEST_FIXTURE_ALPHA", "TEST_FIXTURE_BETA"]
FINAL_SCHEMA = clean_schema(IDS, structured=True, target_ids=["memory"])
_SOCKET_CONNECT = socket.socket.connect


def annotated(identity, number):
    span, subject = identity + "_SPAN", identity + "_SUBJECT"
    return {"id": identity, "tickers": [], "sentiment": "neutral", "sentiment_score": 0,
        "events": ["macro"], "summary_zh": "离线夹具：保留计数事实，时间与币种未知。", "relevance": .5,
        "subjects": [{"id": subject, "type": "industry", "name": "TEST_FIXTURE count",
                      "identifiers": [], "span_ids": [span]}],
        "evidence_spans": [{"id": span, "start": None, "end": None,
                            "quote": "TEST_FIXTURE count is " + str(number) + " units."}],
        "event_records": [{"id": identity + "_EVENT", "event_type": "macro", "subject_ids": [subject],
            "is_primary": True, "event_identifiers": [], "report_period": {"kind": "unknown", "value": None, "span_ids": []},
            "event_time": {"precision": "unknown", "value": None, "span_ids": []},
            "effective_time": {"precision": "unknown", "value": None, "span_ids": []},
            "source_span_ids": [span], "claim_kind": "analysis", "target_ids": []}],
        "claims": [{"id": identity + "_CLAIM", "event_id": identity + "_EVENT", "predicate": "count",
            "value": str(number), "unit": "units", "currency": None, "source_span_ids": [span],
            "relation": "supports", "conflict_group": None, "supersedes_claim_id": None}]}


def keyed(rows=None):
    rows = ([annotated(identity, number) for identity, number in zip(IDS, (7, 3), strict=True)]
            if rows is None else rows)
    return {"documents": {row["id"]: {"id": row["id"], "annotation": {
        key: value for key, value in row.items() if key != "id"}} for row in reversed(rows)}}


def native(arguments=None, *, identifier="TEST_FIXTURE_native", change=None):
    if arguments is None:
        arguments = json.dumps(keyed(), ensure_ascii=False)
    response = {"id": identifier, "object": "chat.completion", "model": "deepseek-flash",
        "system_fingerprint": "TEST_FIXTURE-fingerprint", "choices": [{"index": 0,
        "finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"index": 0, "id": "call_TEST_FIXTURE", "type": "function", "function": {
                "name": CLEAN_TOOL_FUNCTION, "arguments": arguments}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
    if change:
        change(response)
    return response


@contextmanager
def real_http(monkeypatch, produce):
    """Use Requests' actual streaming code and raw.read1 against a real socket."""
    wires, replies, failures, destinations, urls = [], {}, [], [], []
    native_connect = _SOCKET_CONNECT

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                wires.append((self.path, raw))
                response = produce(self.path, body, len(wires))
                wire = response if isinstance(response, bytes) else json_bytes(response)
                replies[hashlib.sha256(wire).hexdigest()] = wire
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.send_header("Content-Encoding", "identity")
                self.end_headers()
                self.wfile.write(wire)
                self.wfile.flush()
            except BaseException as error:
                failures.append(type(error).__name__)
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    pool = HTTPConnectionPool(*server.server_address, maxsize=1)

    class LoopbackAdapter(HTTPAdapter):
        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            assert request.url.startswith("https://api.deepseek.com/") and verify is True
            urls.append(request.url)
            return pool

        def cert_verify(self, connection, url, verify, cert):
            # The fixture deliberately uses plain loopback HTTP. Production's
            # TLS verification flag and Requests' routing metadata are retained.
            assert connection is pool and verify is True

    session = requests.Session()
    session.trust_env = False
    session.mount("https://api.deepseek.com/", LoopbackAdapter())

    def connect_only_loopback(sock, address):
        assert address == server.server_address
        destinations.append(address)
        return native_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect_only_loopback)
    monkeypatch.setattr(requests, "post", session.post)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield {"wires": wires, "replies": replies, "destinations": destinations, "urls": urls}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        session.close()
        pool.close()
        assert not thread.is_alive() and not failures


@pytest.fixture
def execution(tmp_path, monkeypatch):
    for name, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": KEY}.items():
        monkeypatch.setenv(name, value)
    context, root, admission = make_execution(tmp_path, contract=TOOL_CONTRACT)
    advance_to(context, "clean.llm_semantic")
    return context, root, admission


def complete(context, **extra):
    return DeepSeekRequiredClient(context).complete_json(system="TEST_FIXTURE strict data only",
        user="TEST_FIXTURE complete original sources", schema_hint={}, schema=FINAL_SCHEMA,
        clean_protocol={"contract": CLEAN_CONTRACT}, **extra)


def saved(context, ref, stage="clean.llm_semantic", role="output"):
    return read_snapshot(context.store.root, ref, run_id=context.run_id, stage=stage, role=role)


def test_real_http_original_arguments_precede_parse_and_lossless_projection(execution, monkeypatch):
    context, _, _ = execution
    from trading_system.llm import deepseek_required
    original_parse = deepseek_required._json_loads
    checkpointed = []
    original_snapshot = context.model_attempt_snapshot

    def checkpoint(value):
        reference = original_snapshot(value)
        checkpointed.append(saved(context, reference))
        return reference

    def parse(value):
        if isinstance(value, str):
            assert checkpointed and checkpointed[-1]["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == value
        return original_parse(value)

    monkeypatch.setattr(context, "model_attempt_snapshot", checkpoint)
    monkeypatch.setattr(deepseek_required, "_json_loads", parse)
    original_arguments = json.dumps(keyed(), ensure_ascii=False, indent=1)
    with real_http(monkeypatch, lambda *_: native(original_arguments)) as http:
        with context.step("clean.llm_semantic", {}) as scope:
            result = complete(context)
            scope.output(result)
            call = scope.io_calls[0]
            inputs = saved(context, call["input_ref"], role="input")
            proof = saved(context, call["output_ref"])
        assert len(http["wires"]) == 1 and http["destinations"]
        request = json.loads(http["wires"][0][1])
        assert request["max_tokens"] == 393216 and "response_format" not in request
        assert inputs["input_token_estimate"] == count_json_tokens(request)
        assert request["tool_choice"]["function"]["name"] == CLEAN_TOOL_FUNCTION
        assert request["tools"][0]["function"]["strict"] is True
        assert "$ref" not in json.dumps(request["tools"]) and "$def" not in json.dumps(request["tools"])
        assert proof["response"]["choices"][0]["finish_reason"] == proof["finish_reason"] == "tool_calls"
        assert proof["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == original_arguments
        assert proof["native_parsed"] == keyed() and [row["id"] for row in result["documents"]] == IDS
        assert result == {"documents": [annotated(identity, number) for identity, number in zip(IDS, (7, 3), strict=True)]}
        attempt = checkpointed[0]
        assert replay_attempt(attempt, inputs)["parsed"] == result
        assert attempt["transport"]["final_response_url"] == "https://api.deepseek.com/beta/chat/completions"
        assert attempt["transport"]["redirected"] is False
        wire = next(iter(http["replies"].values()))
        assert attempt["transport"]["body_sha256"] == hashlib.sha256(wire).hexdigest()
        assert attempt["transport"]["body_bytes"] == len(wire)


@pytest.mark.parametrize("kind,expected_calls,known,code", [
    ("malformed", 2, 2, "INVALID_RESPONSE_JSON"),
    ("missing", 2, 2, "OUTPUT_SCHEMA_MISMATCH"),
    ("wrong_id", 2, 2, "OUTPUT_SCHEMA_MISMATCH"),
    ("extra_field", 2, 2, "OUTPUT_SCHEMA_MISMATCH"),
    ("unknown_not_null", 2, 2, "OUTPUT_SCHEMA_MISMATCH"),
    ("duplicate", 1, 1, "DUPLICATE_RESPONSE_JSON_KEY"),
    ("tool_name", 1, 1, "INCOMPLETE_MODEL_RESPONSE"),
    ("finish_stop", 1, 1, "INCOMPLETE_MODEL_RESPONSE"),
    ("finish_length", 1, 1, "MODEL_OUTPUT_TRUNCATED"),
    ("reasoning", 1, 1, "NON_THINKING_REASONING_CONTENT"),
    ("missing_usage", 1, 0, "USAGE_REQUIRED"),
    ("http_malformed", 1, 0, "INVALID_RESPONSE_JSON"),
])
def test_real_http_failures_keep_known_or_unknown_accounting(execution, monkeypatch, kind, expected_calls, known, code):
    context, root, admission = execution

    def produce(_path, _body, number):
        value = keyed()
        if kind == "http_malformed":
            return b"{bad HTTP JSON"
        if kind == "missing":
            del value["documents"][IDS[0]]
        elif kind == "wrong_id":
            value["documents"][IDS[0]]["id"] = IDS[1]
        elif kind == "extra_field":
            value["documents"][IDS[0]]["annotation"]["extra"] = "TEST_FIXTURE"
        elif kind == "unknown_not_null":
            value["documents"][IDS[0]]["annotation"]["event_records"][0]["event_time"]["value"] = ""
        arguments = ("{bad original arguments" if kind == "malformed" else
                     '{"documents":{},"documents":{}}' if kind == "duplicate" else json.dumps(value))
        response = native(arguments, identifier="TEST_FIXTURE_" + str(number))
        message = response["choices"][0]["message"]
        if kind == "tool_name":
            message["tool_calls"][0]["function"]["name"] = "TEST_FIXTURE_other"
        elif kind == "finish_stop":
            response["choices"][0]["finish_reason"] = "stop"
        elif kind == "finish_length":
            response["choices"][0]["finish_reason"] = "length"
        elif kind == "reasoning":
            message["reasoning_content"] = "TEST_FIXTURE_PRIVATE_REASONING"
        elif kind == "missing_usage":
            response.pop("usage")
        return response

    with real_http(monkeypatch, produce) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert error.value.diagnostic["code"] == code
        assert len(http["wires"]) == expected_calls
        if expected_calls == 2:
            assert http["wires"][0] == http["wires"][1]
    report = verify_partial_intelligence_model(root, admission)
    assert report["schema"] == "tiger.failed-model-accounting/v3"
    assert report["known_native_attempts"] == known and report["unknown_attempts"] == expected_calls - known
    assert report["known_usage"] == ({"prompt_tokens": 100 * known,
        "completion_tokens": 100 * known, "total_tokens": 200 * known} if known else None)
    assert report["business_completed"] is False and report["all_required_completed"] is False
    assert report["requires_reconciliation"] is (expected_calls != known)
    assert "TEST_FIXTURE_PRIVATE_REASONING" not in "\n".join(path.read_text() for path in (root / "evidence").glob("*.json"))


@pytest.mark.parametrize("escaped", [False, True])
def test_safety_redaction_rejects_changed_original_arguments_without_retry(execution, monkeypatch, escaped):
    context, root, admission = execution
    value = keyed()
    value["documents"][IDS[0]]["annotation"]["summary_zh"] = KEY
    text = json.dumps(value)
    if escaped:
        text = text.replace(KEY, "".join("\\u%04x" % ord(char) for char in KEY))
    with real_http(monkeypatch, lambda *_: native(text)) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert len(http["wires"]) == 1 and error.value.diagnostic["code"] == "RESPONSE_OBJECT_MISMATCH"
    report = verify_partial_intelligence_model(root, admission)
    assert report["known_usage"]["total_tokens"] == 200 and report["format_reanswers"] == 0
    serialized = "\n".join(path.read_text() for path in (root / "evidence").glob("*.json"))
    assert KEY not in serialized


@pytest.mark.parametrize("location", ["tool_id", "function_name", "extra_value", "extra_key", "choice_index", "finish_reason"])
@pytest.mark.parametrize("encoding", ["literal", "unicode", "percent", "html"])
def test_invalid_native_metadata_cannot_persist_encoded_credentials(execution, monkeypatch, location, encoding):
    context, root, admission = execution
    encoded = {"literal": KEY,
        "unicode": "".join("\\u%04x" % ord(char) for char in KEY),
        "percent": "".join("%%%02x" % ord(char) for char in KEY),
        "html": "".join("&#%d;" % ord(char) for char in KEY)}[encoding]
    response = native()
    tool = response["choices"][0]["message"]["tool_calls"][0]
    if location == "tool_id":
        tool["id"] = encoded
    elif location == "function_name":
        tool["function"]["name"] = encoded
    elif location == "extra_value":
        tool["unexpected"] = {"nested": [encoded]}
    elif location == "choice_index":
        response["choices"][0]["index"] = {encoded: [encoded]}
    elif location == "finish_reason":
        response["choices"][0]["finish_reason"] = [encoded, {encoded: encoded}]
    else:
        tool["unexpected"] = {encoded: "TEST_FIXTURE invalid key"}
    with real_http(monkeypatch, lambda *_: response) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert len(http["wires"]) == 1 and error.value.observation["retry_used"] is False
    report = verify_partial_intelligence_model(root, admission)
    assert report["known_usage"]["total_tokens"] == 200 and report["format_reanswers"] == 0
    attempt = saved(context, report["observed_attempts"][0]["observation_ref"])
    assert attempt["content_redacted"] is True
    serialized = "\n".join(path.read_text() for path in (root / "evidence").glob("*.json"))
    assert KEY not in serialized and encoded not in serialized


@pytest.mark.parametrize("location", ["tool_extra", "choice_index", "finish_reason"])
def test_deep_invalid_native_metadata_keeps_known_usage_and_safe_checkpoint(execution, monkeypatch, location):
    context, root, admission = execution
    response = native()
    nested = KEY
    for _ in range(70):
        nested = {"TEST_FIXTURE_nested": [nested]}
    choice = response["choices"][0]
    if location == "tool_extra":
        choice["message"]["tool_calls"][0]["unexpected"] = nested
    else:
        choice["index" if location == "choice_index" else "finish_reason"] = nested
    with real_http(monkeypatch, lambda *_: response) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert len(http["wires"]) == 1 and error.value.observation["retry_used"] is False
    report = verify_partial_intelligence_model(root, admission)
    assert report["known_usage"]["total_tokens"] == 200 and report["known_native_attempts"] == 1
    assert report["unknown_attempts"] == 0 and report["format_reanswers"] == 0
    attempt = saved(context, report["observed_attempts"][0]["observation_ref"])
    assert attempt["content_redacted"] is True and attempt["response"]["usage"]["total_tokens"] == 200
    assert KEY not in "\n".join(path.read_text() for path in (root / "evidence").glob("*.json"))


@pytest.mark.parametrize("location", ["value", "key"])
def test_invalid_unicode_native_metadata_keeps_known_usage_and_checkpoint(execution, monkeypatch, location):
    context, root, admission = execution
    response = native()
    tool = response["choices"][0]["message"]["tool_calls"][0]
    tool["unexpected"] = {"TEST_FIXTURE": "\ud800"} if location == "value" else {"\ud800": "TEST_FIXTURE"}
    # ASCII JSON escapes can carry an invalid Unicode scalar through the HTTP
    # JSON decoder. The safe projection must reject it before UTF-8 hashing.
    wire = json.dumps(response, ensure_ascii=True).encode("ascii")
    with real_http(monkeypatch, lambda *_: wire) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert len(http["wires"]) == 1 and error.value.observation["retry_used"] is False
    report = verify_partial_intelligence_model(root, admission)
    assert report["known_usage"]["total_tokens"] == 200 and report["known_native_attempts"] == 1
    assert report["unknown_attempts"] == 0 and report["format_reanswers"] == 0
    attempt = saved(context, report["observed_attempts"][0]["observation_ref"])
    assert attempt["content_redacted"] is True


def test_numeric_overflow_in_native_metadata_keeps_known_usage_and_checkpoint(execution, monkeypatch):
    context, root, admission = execution
    response = native()
    response["choices"][0]["message"]["tool_calls"][0]["unexpected"] = {"nested": "TEST_FIXTURE_REPLACE_NUMBER"}
    # This is a JSON numeric literal, not an Infinity/NaN constant. Python's
    # standard decoder produces float infinity, so safe projection owns it.
    wire = json.dumps(response).replace('"TEST_FIXTURE_REPLACE_NUMBER"', '1e999').encode("ascii")
    with real_http(monkeypatch, lambda *_: wire) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert len(http["wires"]) == 1 and error.value.observation["retry_used"] is False
    report = verify_partial_intelligence_model(root, admission)
    assert report["known_usage"]["total_tokens"] == 200 and report["known_native_attempts"] == 1
    assert report["unknown_attempts"] == 0 and report["format_reanswers"] == 0
    attempt = saved(context, report["observed_attempts"][0]["observation_ref"])
    assert attempt["content_redacted"] is True


def test_checkpoint_write_failure_stops_before_argument_parse_or_another_send(execution, monkeypatch):
    context, _, _ = execution
    from trading_system.llm import deepseek_required
    original_parse = deepseek_required._json_loads

    def parse(value):
        assert isinstance(value, bytes), "Arguments parsed before durable checkpoint"
        return original_parse(value)

    def unavailable(_value):
        raise EvidenceWriteError("TEST_FIXTURE native checkpoint unavailable")

    monkeypatch.setattr(deepseek_required, "_json_loads", parse)
    monkeypatch.setattr(context, "model_attempt_snapshot", unavailable)
    with real_http(monkeypatch, lambda *_: native("{bad")) as http:
        with pytest.raises(EvidenceWriteError):
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert len(http["wires"]) == 1


def test_original_final_schema_failure_cannot_be_hidden_by_provider_omission(execution, monkeypatch):
    context, _, _ = execution
    value = keyed()
    value["documents"][IDS[0]]["annotation"]["summary_zh"] = "测" * 601
    with real_http(monkeypatch, lambda _p, _b, n: native(json.dumps(value), identifier="TEST_FIXTURE_" + str(n))) as http:
        with pytest.raises(RequiredModelUnavailable) as error:
            with context.step("clean.llm_semantic", {}):
                complete(context)
        assert error.value.diagnostic["schema_keyword"] == "maxLength" and len(http["wires"]) == 2


def test_no_caller_cap_or_unadmitted_protocol_can_dispatch(execution, monkeypatch):
    context, _, _ = execution
    sent = []
    monkeypatch.setattr(requests, "post", lambda *_a, **_k: sent.append(True))
    with pytest.raises(RequiredModelUnavailable) as error:
        with context.step("clean.llm_semantic", {}):
            complete(context, max_tokens=1000)
    assert error.value.diagnostic["code"] == "CALLER_OUTPUT_CAP_FORBIDDEN" and sent == []


def test_coherent_v3_policy_cannot_count_a_v2_request_and_native_attempt(execution):
    """A rehashed observation label cannot upgrade a v2 reply's route proof."""
    import test_intelligence_json_observation_proof as legacy
    context, root, admission = execution
    inputs = legacy.request("clean.llm_semantic")
    original_v2 = legacy.native(inputs, 1, "{bad original v2 JSON")
    with pytest.raises(RequiredModelUnavailable):
        with context.step("clean.llm_semantic", {}):
            raise legacy.emit(context, inputs, [original_v2],
                change_outer=lambda value: value.update(schema=TOOL_CONTRACT))
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(root, admission)


def test_global_reanswer_cannot_reset_between_tool_and_content_clients(execution, monkeypatch):
    from test_deepseek_required import SCHEMA
    context, root, admission = execution

    def produce(path, _body, number):
        if path == "/beta/chat/completions":
            return native("{bad" if number == 1 else None, identifier="TEST_FIXTURE_" + str(number))
        assert path == "/chat/completions" and number == 3
        response = native(identifier="TEST_FIXTURE_later")
        response["choices"][0].update(finish_reason="stop", message={"role": "assistant", "content": "{bad later JSON"})
        return response

    with real_http(monkeypatch, produce) as http:
        with context.step("clean.llm_semantic", {}) as scope:
            scope.output(complete(context))
        while context._specs[context._cursor].name != "tech.sentiment":
            stage = context._specs[context._cursor].name
            with context.step(stage, {"TEST_FIXTURE_input": stage}) as scope:
                scope.output({"TEST_FIXTURE_output": stage})
        with pytest.raises(RequiredModelUnavailable):
            with context.step("tech.sentiment", {}):
                DeepSeekRequiredClient(context).complete_json(system="TEST_FIXTURE", user="TEST_FIXTURE",
                    schema_hint={}, schema=SCHEMA)
        assert len(http["wires"]) == 3 and http["wires"][0] == http["wires"][1]
    report = verify_partial_intelligence_model(root, admission)
    assert report["known_usage"]["total_tokens"] == 600 and report["format_reanswers"] == 1
    assert report["unknown_attempts"] == 0 and report["stage_counts"]["clean.llm_semantic"] == 1


@pytest.mark.parametrize("members,expected", [
    ([], ""),
    (["trading_system/llm/json_observation.py"], "tiger.deepseek-json-observation/v2"),
    (["trading_system/llm/json_observation.py", "trading_system/llm/clean_keyed_codec.py",
      "trading_system/llm/strict_clean_transport.py"], TOOL_CONTRACT),
])
def test_actual_frozen_membership_selects_explicit_version(members, expected):
    from trading_system.agent_runtime import INTELLIGENCE_PROFILE, PROFILE, source_observation_contract
    files = [{"path": name} for name in members]
    assert source_observation_contract(files, INTELLIGENCE_PROFILE) == expected
    assert source_observation_contract(files, PROFILE) == ""


@pytest.mark.parametrize("members", [
    ["trading_system/llm/clean_keyed_codec.py"],
    ["trading_system/llm/strict_clean_transport.py"],
    ["trading_system/llm/json_observation.py", "trading_system/llm/clean_keyed_codec.py"],
    ["trading_system/llm/clean_keyed_codec.py", "trading_system/llm/strict_clean_transport.py"],
])
def test_partial_new_source_membership_cannot_activate_any_protocol(members):
    from trading_system.agent_runtime import INTELLIGENCE_PROFILE, source_observation_contract
    with pytest.raises(EvidenceInvalid):
        source_observation_contract([{"path": name} for name in members], INTELLIGENCE_PROFILE)


def rewrite_success_proof(context, root, transform):
    """Rehash the actual mid-stream clean output and every successor link."""
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    end = next(row for row in events if row["stage"] == "clean.llm_semantic" and row["kind"] == "end")
    output = saved(context, end["output_ref"])
    call = next(row for row in output["io_calls"] if row["method"] == "model.deepseek.chat")
    proof = saved(context, call["output_ref"])
    transform(proof)
    call["output_ref"] = context.store.snapshot(stage="clean.llm_semantic", role="output", value=proof)
    end["output_ref"] = context.store.snapshot(stage="clean.llm_semantic", role="output", value=output)
    previous_event = previous_stage = None
    for offset in range(0, len(events), 2):
        begin, terminal = events[offset:offset + 2]
        for row in (begin, terminal):
            row["previous_stage_ref"] = previous_stage
            row["previous_event_sha256"] = previous_event
            row["event_sha256"] = hashlib.sha256(json_bytes({key: value for key, value in row.items()
                                                            if key != "event_sha256"})).hexdigest()
            previous_event = row["event_sha256"]
        previous_stage = terminal["output_ref"]
    (root / "events.jsonl").write_bytes(b"".join(json_bytes(row) for row in events))


@pytest.mark.parametrize("outcome", ["success_after_retry", "known_failure", "unknown_second"])
def test_real_http_five_stages_current_v3_full_and_partial_independent_proof(tmp_path, monkeypatch, outcome):
    """Exercise actual main, data acquisition, strict transport and both proofs."""
    import main
    from trading_system.llm import intelligence_required as semantic
    from trading_system.agent_runtime import INTELLIGENCE_PROFILE, policy_manifest
    from trading_system.redline.execution import ExecutionContext
    from trading_system.paper_proof import verify_paper_execution
    from test_intelligence_workflow_v2 import intelligence_fixture, answer

    real_post_json = DeepSeekRequiredClient._post_json
    original, root, args, _, denied, _ = intelligence_fixture(tmp_path, monkeypatch,
        authoritative=True, clean_format_contract=semantic.CLEAN_FORMAT_CONTRACT)
    context = ExecutionContext(run_id=original.run_id, run_date=original.run_date, paths=original.paths,
        policy=original.policy, store=original.store, admitted_at=original.admitted_at,
        authoritative_paper=True, response_observation_contract=TOOL_CONTRACT)
    packet = read_json(root, "intelligence-input.json")
    atomic_json(root, "policy.json", policy_manifest(INTELLIGENCE_PROFILE, authoritative_paper=True,
        intelligence_input=packet, response_observation_contract=TOOL_CONTRACT))
    admission = {"clock": {"run_date": context.run_date.isoformat(), "market_timezone": "America/New_York",
        "admitted_at_utc": context.admitted_at.isoformat()}}
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", real_post_json)
    clean_calls = []

    def produce(path, body, number):
        if path == "/v1/tools/search":
            key = hashlib.sha256(body["query"].encode()).hexdigest()
            return {"results": [{"url": "https://www.reuters.com/fixture/" + key,
                "title": "TEST_FIXTURE loopback " + key, "content": "Discovery text " + key,
                "date": "2026-10-19T14:00:00Z"}]}
        assert path in {"/chat/completions", "/beta/chat/completions"}
        content = body["messages"][1]["content"]
        marker = ("\nOriginal final acceptance JSON schema (after lossless flatten):\n"
                  if path == "/beta/chat/completions" else "\nJSON schema:\n")
        schema = json.loads(content.split(marker)[1].split("\nJSON field guidance:\n")[0])
        payload = json.loads(content.split("\n", 1)[1].split(marker)[0])
        properties = schema["properties"]
        stage = ("clean.llm_semantic" if "documents" in properties else
                 "tech.sentiment" if "chains" in properties else "tech.risk" if "chain_assessments" in properties
                 else "sector.narrative" if "sectors" in properties else "decision.debate")
        result = answer(stage, schema, payload)
        if stage == "clean.llm_semantic":
            assert path == "/beta/chat/completions"
            clean_calls.append(body)
            if len(clean_calls) == 2:
                checkpoint = [json.loads(file.read_text())["value"] for file in (root / "evidence").glob("*.json")]
                first = [value for value in checkpoint if isinstance(value, dict) and
                         value.get("schema") == "tiger.deepseek-observed-attempt/v3"]
                assert len(first) == 1 and first[0]["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{TEST_FIXTURE malformed original arguments"
            bad = len(clean_calls) == 1 or outcome != "success_after_retry"
            arguments = "{TEST_FIXTURE malformed original arguments" if bad else json.dumps(keyed(result["documents"]), ensure_ascii=False, indent=1)
            response = native(arguments, identifier="TEST_FIXTURE_tcp_" + str(number))
            if outcome == "unknown_second" and len(clean_calls) == 2:
                response.pop("usage")
            return response
        return {"id": "TEST_FIXTURE_tcp_" + str(number), "object": "chat.completion", "model": "deepseek-flash",
            "system_fingerprint": "TEST_FIXTURE-fingerprint", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}

    with real_http(monkeypatch, produce) as http:
        if outcome == "success_after_retry":
            actual = main._daily(args, "yahoo", execution=context)
        else:
            with pytest.raises(RequiredModelFailed):
                main._daily(args, "yahoo", execution=context)
        assert http["destinations"] and denied == []
        assert clean_calls[0] == clean_calls[1]
        replies = dict(http["replies"])
        chat = [wire for wire in http["wires"] if wire[0] in {"/chat/completions", "/beta/chat/completions"}]
    transport_root = tmp_path / "http-fixture-bodies"
    transport_root.mkdir()
    for body_hash, wire in replies.items():
        (transport_root / (body_hash + ".json")).write_bytes(wire)
    fixture_metadata = {"scope": "OFFLINE_TEST_FIXTURE_ONLY", "run": str(root), "admission": admission,
        "response_observation_contract": TOOL_CONTRACT, "actual_network_destinations": "loopback only",
        "provider_calls": 0, "tls_or_financial_quality_certified": False}
    monkeypatch.setattr(requests, "post", lambda *_a, **_k: pytest.fail("Independent proof attempted network"))
    if outcome != "success_after_retry":
        report = verify_partial_intelligence_model(root, admission)
        assert report["known_usage"]["total_tokens"] == (200 if outcome == "unknown_second" else 400)
        assert report["unknown_attempts"] == int(outcome == "unknown_second")
        assert report["business_completed"] is False and report["format_reanswers"] == 1
        assert not (root / "receipt.json").exists() and not (root / "account-version.json").exists()
        (tmp_path / "model-fixture.json").write_bytes(json_bytes({**fixture_metadata, "accounting": report}))
        return
    context.assert_complete()
    result = asdict(actual.result)
    summary = verify_intelligence_model(root, result, admission, response_observation_contract=TOOL_CONTRACT)
    paper = verify_paper_execution(root, {**admission, "parent": None}, result, actual.sim_state)
    accounting = summary["chat_accounting"]
    assert all(summary["stage_counts"].values()) and len(chat) == summary["call_count"] + 1
    assert accounting["known_usage"]["total_tokens"] == 200 * len(chat)
    assert accounting["unknown_attempts"] == 0 and accounting["format_reanswers"] == 1
    assert {key: value for key, value in summary.items() if key != "chat_accounting"} == result["raw"]["model"]
    assert paper["contract"] == "tiger.paper-execution.v1"
    (tmp_path / "model-fixture.json").write_bytes(json_bytes({**fixture_metadata,
        "result": result, "model_proof": summary, "paper_state": actual.sim_state, "paper_proof": paper}))
    for observed in accounting["observed_attempts"]:
        value = saved(context, observed["observation_ref"], stage=observed["stage"])
        transport = value["transport"]
        wire = replies[transport["body_sha256"]]
        expected_path = "/beta/chat/completions" if observed["stage"] == "clean.llm_semantic" else "/chat/completions"
        assert transport["body_bytes"] == len(wire) and transport["body_sha256"] == hashlib.sha256(wire).hexdigest()
        assert transport["final_response_url"] == "https://api.deepseek.com" + expected_path and transport["redirected"] is False
        expected_finish = "tool_calls" if observed["stage"] == "clean.llm_semantic" else "stop"
        assert value["response"]["choices"][0]["finish_reason"] == expected_finish

    original_events = (root / "events.jsonl").read_bytes()
    mutations = {
        "business_projection": lambda proof: proof["parsed"]["documents"][0].update(summary_zh="被篡改的合法中文说明"),
        "native_projection": lambda proof: next(iter(proof["native_parsed"]["documents"].values()))["annotation"].update(summary_zh="被篡改的合法中文说明"),
        "synthetic_stop": lambda proof: proof.update(finish_reason="stop"),
        "protocol_downgrade": lambda proof: proof.update(clean_protocol={"contract": "typed-clean-complete-format/v1"}),
    }

    def changed_arguments(proof):
        proof["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(proof["native_parsed"])
        from trading_system.llm.json_observation import digest
        proof["response_sha256"] = digest(proof["response"])

    def changed_route(proof):
        observed = proof["response_observation"]["attempts"][-1]
        value = saved(context, observed["observation_ref"])
        value["transport"]["final_response_url"] = "https://api.deepseek.com/chat/completions"
        observed["observation_ref"] = context.store.snapshot(stage="clean.llm_semantic", role="output", value=value)

    mutations.update(original_arguments_changed=changed_arguments, native_route_downgrade=changed_route)
    for mutation, transform in mutations.items():
        (root / "events.jsonl").write_bytes(original_events)
        rewrite_success_proof(context, root, transform)
        with pytest.raises(EvidenceInvalid):
            verify_intelligence_model(root, result, admission, response_observation_contract=TOOL_CONTRACT)
    (root / "events.jsonl").write_bytes(original_events)
    assert verify_intelligence_model(root, result, admission, response_observation_contract=TOOL_CONTRACT) == summary
