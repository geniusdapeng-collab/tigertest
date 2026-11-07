"""TEST_FIXTURE native refs: failed accounting is never business completion."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json

import pytest

from trading_system.agent_evidence import (EvidenceInvalid, EvidenceStore, atomic_json, atomic_bytes,
    read_json, read_snapshot, verify_events, json_bytes, digest as byte_digest)
from trading_system.llm.deepseek_required import MODEL, RELEASE, SEARCH_MODE
from trading_system.llm.intelligence_proof import (verify_intelligence_model,
    verify_partial_intelligence_model)
from trading_system.llm.json_observation import CONTRACT, digest, replay_attempt, staged_diagnostic
from trading_system.llm.model_budget import (POLICY_VERSION, REGISTRY_VERSION,
    TOKENIZER_SHA256, chat_request_core, plan_request)
from trading_system.llm.scene_policy import KERNEL_SCENE_MAP, scene_config
from trading_system.redline.execution import (ExecutionContext, ExecutionContractError,
    ExecutionPolicy, INTELLIGENCE_POLICY_ID, RuntimePaths, daily_stage_contract,
    daily_stage_specs)
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


def make_execution(tmp_path, *, contract=CONTRACT):
    root = tmp_path / "run-TEST_FIXTURE-native-observation"
    work = root / "work"
    work.mkdir(parents=True)
    paths = RuntimePaths(work_dir=work, reports_dir=work / "reports",
        journal_path=work / "reports/journal.json", sim_path=work / "reports/sim.json",
        calibration_path=work / "reports/calibration.json", options_history_dir=work / "reports/options",
        governance_path=work / "reports/governance.jsonl", proposals_dir=work / "reports/proposals",
        calendar_override_path=work / "calendar.json")
    execution = ExecutionContext(run_id=root.name, run_date=date(2026, 10, 20), paths=paths,
        policy=ExecutionPolicy(INTELLIGENCE_POLICY_ID, model_mode="enabled",
            search_source_names=("deepseek_search",)), store=EvidenceStore(root, root.name),
        admitted_at=datetime(2026, 10, 20, 22, tzinfo=timezone.utc),
        response_observation_contract=contract)
    policy = {"stages": daily_stage_contract(True, intelligence=True),
        "model_contract": {"response_observation_contract": contract}}
    atomic_json(root, "policy.json", policy)
    admission = {"clock": {"run_date": "2026-10-20", "market_timezone": "America/New_York",
        "admitted_at_utc": execution.admitted_at.isoformat()}}
    return execution, root, admission


def advance_to(execution, stage):
    for spec in daily_stage_specs(True, intelligence=True):
        if spec.name == stage:
            return
        with execution.step(spec.name, {"TEST_FIXTURE_input": spec.name}) as scope:
            scope.output({"TEST_FIXTURE_output": spec.name})
    raise AssertionError(stage)


def request(stage, number=1):
    thinking = scene_config(KERNEL_SCENE_MAP[stage])["thinking"]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    body, estimate, ceiling = plan_request(chat_request_core(system="TEST_FIXTURE system",
        user="TEST_FIXTURE complete source", schema=schema, schema_hint={},
        thinking_mode=thinking["type"], effort=thinking.get("effort"), temperature=.2))
    return {"schema": "tiger.deepseek-request.v2", "response_observation_contract": CONTRACT,
        "call_id": f"{number:032x}", "stage": stage, "provider": "deepseek", "method": "POST",
        "endpoint": "https://api.deepseek.com/chat/completions", "request": body,
        "request_sha256": digest(body), "output_schema": schema, "schema_sha256": digest(schema),
        "release": RELEASE, "scene": KERNEL_SCENE_MAP[stage], "thinking_mode": thinking["type"],
        "reasoning_effort": thinking.get("effort"), "input_token_estimate": estimate,
        "output_ceiling": ceiling, "policy_version": POLICY_VERSION,
        "registry_version": REGISTRY_VERSION, "tokenizer_sha256": TOKENIZER_SHA256}


def native(inputs, ordinal, content='{"answer":"TEST_FIXTURE valid"}', *, unknown=False, response_id=None):
    transport = {"http_status": 200, "body_sha256": hashlib.sha256(
        f"TEST_FIXTURE wire {inputs['call_id']} {ordinal}".encode()).hexdigest(),
        "body_bytes": 100, "body_whitespace_only": False, "body_complete": True}
    response = {"id": response_id or f"fixture-{inputs['call_id']}-{ordinal}", "object": "chat.completion",
        "model": MODEL, "system_fingerprint": "fp-TEST_FIXTURE",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100 * ordinal, "completion_tokens": 100 * ordinal,
                  "total_tokens": 200 * ordinal}}
    diagnostic = staged_diagnostic(RequiredModelUnavailable("INVALID_RESPONSE_JSON"), "http_envelope", transport)
    return {"schema": "tiger.deepseek-observed-attempt/v2", "call_id": inputs["call_id"],
        "ordinal": ordinal, "request_sha256": inputs["request_sha256"], "transport": transport,
        "response": None if unknown else response, "response_sha256": None if unknown else digest(response),
        "content_redacted": None if unknown else False, "reasoning_state": None if unknown else "absent",
        "fingerprint_valid": None if unknown else True, "transport_diagnostic": diagnostic if unknown else None}


def emit(execution, inputs, natives, *, failed=True, change_outer=None):
    handle = execution.begin_io("model.deepseek.chat", inputs)
    attempts = []
    for item in natives:
        ref = execution.model_attempt_snapshot(item)
        result = replay_attempt(item, inputs)
        attempts.append({"ordinal": item["ordinal"], "observation_ref": ref,
                         "diagnostic": result["diagnostic"]})
    observation = {"schema": CONTRACT, "call_id": inputs["call_id"],
        "request_sha256": inputs["request_sha256"], "attempts": attempts, "retry_used": len(attempts) == 2}
    if change_outer:
        change_outer(observation)
    if failed:
        diagnostic = attempts[-1]["diagnostic"]
        assert diagnostic is not None
        error = RequiredModelUnavailable(diagnostic["code"], json_stage=diagnostic["json_stage"],
            transport=diagnostic.get("transport"), schema_path=diagnostic.get("schema_path"),
            schema_keyword=diagnostic.get("schema_keyword"), observation=observation)
        execution.finish_io(handle, error=error)
        return error
    final = natives[-1]
    parsed = replay_attempt(final, inputs)["parsed"]
    response = {key: final["response"][key] for key in ("id", "object", "model", "system_fingerprint", "usage")}
    response["choices"] = [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}}]
    proof = {key: inputs[key] for key in ("call_id", "stage", "provider", "release", "scene",
        "thinking_mode", "reasoning_effort", "input_token_estimate", "output_ceiling", "policy_version",
        "registry_version", "tokenizer_sha256", "request_sha256", "schema_sha256")}
    proof.update(schema="tiger.deepseek-call.v2", response_observation_contract=CONTRACT, requested_model=MODEL, model=MODEL,
        response_id=response["id"], usage=response["usage"], finish_reason="stop",
        response_sha256=digest(response), transport_response_sha256=final["transport"]["body_sha256"],
        response=response, system_fingerprint=response["system_fingerprint"], search_mode=SEARCH_MODE,
        parsed=parsed, response_observation=observation)
    execution.finish_io(handle, output=proof)
    return proof


def fail_run(execution, inputs, natives, *, change_outer=None):
    stage = inputs["stage"]
    advance_to(execution, stage)
    with pytest.raises(RequiredModelUnavailable):
        with execution.step(stage, {"TEST_FIXTURE": "failed accounting only"}):
            raise emit(execution, inputs, natives, change_outer=change_outer)


def test_failed_stage_binds_both_known_attempts_and_sums_original_usage(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    fail_run(execution, inputs, [native(inputs, 1, "{broken"), native(inputs, 2, "{still broken")])
    result = verify_partial_intelligence_model(root, admission)
    assert result["known_usage"] == {"prompt_tokens": 300, "completion_tokens": 300, "total_tokens": 600}
    assert result["unknown_attempts"] == 0 and result["format_reanswers"] == 1
    assert result["business_completed"] is False and result["all_required_completed"] is False
    assert len(result["observed_attempts"]) == 2 and execution.summary()[-1]["status"] == "failed"
    with pytest.raises(EvidenceInvalid):
        verify_events(root, root.name, daily_stage_contract(True, intelligence=True))
    with pytest.raises(EvidenceInvalid):
        verify_intelligence_model(root, {"raw": {}}, admission)
    assert not (root / "account-version.json").exists() and not (root / "receipt.json").exists()


def test_second_unknown_keeps_first_known_usage_and_requires_reconciliation(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    fail_run(execution, inputs, [native(inputs, 1, " "), native(inputs, 2, unknown=True)])
    result = verify_partial_intelligence_model(root, admission)
    assert result["known_usage"]["total_tokens"] == 200
    assert result["unknown_attempts"] == 1 and result["requires_reconciliation"] is True
    assert result["observed_attempts"][1]["usage"] is None


def test_native_snapshot_requires_unique_active_unfinished_chat_io(tmp_path):
    execution, _, _ = make_execution(tmp_path)
    with pytest.raises(ExecutionContractError):
        execution.model_attempt_snapshot({"TEST_FIXTURE": "outside stage"})
    execution, _, _ = make_execution(tmp_path / "next")
    with pytest.raises(ExecutionContractError):
        with execution.step("daily.preflight", {}):
            execution.begin_io("provider.yahoo.ohlcv", {})
            execution.model_attempt_snapshot({"TEST_FIXTURE": "wrong IO"})


def test_global_format_retry_belongs_to_execution_across_client_instances(tmp_path):
    execution, _, _ = make_execution(tmp_path)
    with execution.step("daily.preflight", {}) as scope:
        assert execution.claim_model_format_retry() is True
        assert execution.claim_model_format_retry() is False
        scope.output({})
    with execution.step("state.reset", {}) as scope:
        assert execution.claim_model_format_retry() is False
        scope.output({})


def test_unknown_attempt_forbids_any_later_chat_even_if_original_error_is_caught(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    stage = "clean.llm_semantic"
    advance_to(execution, stage)
    with pytest.raises(RequiredModelUnavailable):
        with execution.step(stage, {}):
            first = request(stage)
            emit(execution, first, [native(first, 1, unknown=True)])
            second = request(stage, 2)
            raise emit(execution, second, [native(second, 1, "{bad")])
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(root, admission)


def test_global_retry_count_cannot_reset_in_a_later_call_or_stage(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    stage = "clean.llm_semantic"
    advance_to(execution, stage)
    with pytest.raises(RequiredModelUnavailable):
        with execution.step(stage, {}):
            first = request(stage)
            emit(execution, first, [native(first, 1, "{bad"), native(first, 2)], failed=False)
            second = request(stage, 2)
            raise emit(execution, second, [native(second, 1, "{bad"), native(second, 2, "{bad")])
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(root, admission)


def test_failed_after_output_keeps_existing_output_ref_but_still_cannot_complete(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    advance_to(execution, inputs["stage"])
    with pytest.raises(RuntimeError):
        with execution.step(inputs["stage"], {}) as scope:
            emit(execution, inputs, [native(inputs, 1)], failed=False)
            scope.output({"TEST_FIXTURE": "saved before later failure"})
            raise RuntimeError("TEST_FIXTURE later failure")
    result = verify_partial_intelligence_model(root, admission)
    assert result["known_usage"]["total_tokens"] == 200 and result["business_completed"] is False


def test_legacy_failure_keeps_null_output_and_no_native_contract(tmp_path):
    execution, root, _ = make_execution(tmp_path, contract="")
    with pytest.raises(RuntimeError):
        with execution.step("daily.preflight", {}):
            raise RuntimeError("TEST_FIXTURE legacy failure")
    assert execution.summary()[-1]["output_ref"] is None
    events = (root / "events.jsonl").read_text()
    assert "tiger.failed-stage-output/v2" not in events and CONTRACT not in events


@pytest.mark.parametrize("contract", [None, [], "unknown", 1])
def test_unknown_or_malformed_contract_cannot_activate_observation(tmp_path, contract):
    with pytest.raises(ExecutionContractError):
        make_execution(tmp_path, contract=contract)


def business_fixture(tmp_path, monkeypatch, *, authoritative=False):
    from test_intelligence_workflow_v2 import intelligence_fixture
    from trading_system.agent_runtime import policy_manifest
    from trading_system.redline.execution import INTELLIGENCE_PROFILE

    old, root, args, _, _, calls = intelligence_fixture(tmp_path, monkeypatch, authoritative=authoritative)
    execution = ExecutionContext(run_id=old.run_id, run_date=old.run_date, paths=old.paths,
        policy=old.policy, store=old.store, admitted_at=old.admitted_at, authoritative_paper=authoritative,
        response_observation_contract=CONTRACT)
    packet = read_json(root, "intelligence-input.json")
    atomic_json(root, "policy.json", policy_manifest(INTELLIGENCE_PROFILE,
        authoritative_paper=authoritative, intelligence_input=packet, response_observation_contract=CONTRACT))
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
        "admitted_at_utc": execution.admitted_at.isoformat()}}
    return execution, root, args, admission, calls


@pytest.mark.parametrize("authoritative", [False, True])
def test_actual_five_stage_fixture_format_retry_preserves_full_proof_and_original_reply(tmp_path, monkeypatch, authoritative):
    import main
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient

    execution, root, args, admission, calls = business_fixture(tmp_path, monkeypatch, authoritative=authoritative)
    original = DeepSeekRequiredClient._post_json
    first = []
    def post(client, path, body):
        value, _ = original(client, path, body)
        if path == "/chat/completions" and not first:
            first.append(value["id"])
            value["choices"][0]["message"]["content"] = "TEST_FIXTURE malformed assistant JSON"
        return value, digest(value)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    actual = main._daily(args, "yahoo", execution=execution)
    result = asdict(actual.result)
    summary = verify_intelligence_model(root, result, admission)
    assert summary["all_required_completed"] is True and all(summary["stage_counts"].values())
    assert len([row for row in calls if row[1] == "/chat/completions"]) == summary["call_count"] + 1
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    end = next(row for row in events if row["stage"] == "clean.llm_semantic" and row["kind"] == "end")
    output = read_snapshot(root, end["output_ref"], run_id=root.name, stage=end["stage"], role="output")
    call = next(row for row in output["io_calls"] if row["method"] == "model.deepseek.chat")
    proof = read_snapshot(root, call["output_ref"], run_id=root.name, stage=end["stage"], role="output")
    attempt = proof["response_observation"]["attempts"][0]
    native_value = read_snapshot(root, attempt["observation_ref"], run_id=root.name, stage=end["stage"], role="output")
    assert native_value["response"]["id"] == first[0]
    assert native_value["response"]["choices"][0]["message"]["content"] == "TEST_FIXTURE malformed assistant JSON"
    assert attempt["diagnostic"]["code"] == "INVALID_RESPONSE_JSON"
    if authoritative:
        from trading_system.paper_proof import verify_paper_execution
        paper = verify_paper_execution(root, {**admission, "parent": None}, result, actual.sim_state)
        assert paper["contract"] == "tiger.paper-execution.v1"


@pytest.mark.parametrize("unknown_second", [False, True])
def test_actual_business_failure_preserves_second_attempt_and_known_first_usage(tmp_path, monkeypatch, unknown_second):
    import main
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.redline.execution import RequiredModelFailed

    execution, root, args, admission, _ = business_fixture(tmp_path, monkeypatch)
    original = DeepSeekRequiredClient._post_json
    count = []
    def post(client, path, body):
        if path == "/chat/completions":
            count.append(body)
            if len(count) == 2 and unknown_second:
                raise RequiredModelUnavailable("INVALID_RESPONSE_JSON")
        value, _ = original(client, path, body)
        if path == "/chat/completions":
            value["choices"][0]["message"]["content"] = "TEST_FIXTURE invalid assistant JSON"
        return value, digest(value)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    result = verify_partial_intelligence_model(root, admission)
    assert len(count) == 2 and count[0] == count[1]
    assert result["known_usage"]["total_tokens"] == (200 if unknown_second else 400)
    assert result["unknown_attempts"] == int(unknown_second)
    assert len(result["observed_attempts"]) == 2
    assert result["business_completed"] is False and execution.summary()[-1]["status"] == "failed"
    assert not (root / "account-version.json").exists() and not (root / "receipt.json").exists()


def rewrite_bound_failure(root, transform, store):
    """Deliberately rewrite TEST_FIXTURE refs and the chain coherently."""
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    end = events[-1]
    stage = end["stage"]
    read = lambda ref: read_snapshot(root, ref, run_id=root.name, stage=stage, role="output")
    output = read(end["output_ref"])
    call = next(row for row in output["io_calls"] if row["method"] == "model.deepseek.chat" and "error_ref" in row)
    error = read(call["error_ref"])
    native_values = [read(row["observation_ref"]) for row in error["response_observation"]["attempts"]]
    transform(error, native_values)
    if "response_observation" in error:
        for row, value in zip(error["response_observation"]["attempts"], native_values, strict=True):
            row["observation_ref"] = store.snapshot(stage=stage, role="output", value=value)
    call["error_ref"] = store.snapshot(stage=stage, role="output", value=error)
    end["output_ref"] = store.snapshot(stage=stage, role="output", value=output)
    previous = None
    for row in events:
        row["previous_event_sha256"] = previous
        row["event_sha256"] = byte_digest(json_bytes({key: value for key, value in row.items() if key != "event_sha256"}))
        previous = row["event_sha256"]
    atomic_bytes(root, "events.jsonl", b"".join(json_bytes(row) for row in events))


@pytest.mark.parametrize("mutation", ["request", "ordinal", "model", "usage", "identity", "content",
    "diagnostic", "error_diagnostic", "missing_observation", "known_whitespace", "known_zero_bytes",
    "ineligible_retry", "redacted_retry"])
def test_rehashed_native_failure_mutations_reject_in_independent_partial_verifier(tmp_path, mutation):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    fail_run(execution, inputs, [native(inputs, 1, "{bad"), native(inputs, 2, "{bad again")])
    assert verify_partial_intelligence_model(root, admission)["known_usage"]["total_tokens"] == 600
    def transform(error, values):
        first, second = values
        if mutation == "request":
            first["request_sha256"] = "a" * 64
        elif mutation == "ordinal":
            first["ordinal"] = 2
        elif mutation == "model":
            first["response"]["model"] = "TEST_FIXTURE-other-model"
        elif mutation == "usage":
            first["response"]["usage"]["total_tokens"] += 1
        elif mutation == "identity":
            second["response"]["id"] = first["response"]["id"]
        elif mutation == "content":
            first["response"]["choices"][0]["message"]["content"] = '{"answer":"TEST_FIXTURE now valid"}'
        elif mutation == "diagnostic":
            error["response_observation"]["attempts"][0]["diagnostic"]["code"] = "EMPTY_MODEL_OUTPUT"
        elif mutation == "error_diagnostic":
            error["diagnostic"]["code"] = "EMPTY_MODEL_OUTPUT"
        elif mutation == "missing_observation":
            del error["response_observation"]
        elif mutation == "known_whitespace":
            first["transport"]["body_whitespace_only"] = True
        elif mutation == "known_zero_bytes":
            first["transport"]["body_bytes"] = 0
        elif mutation == "ineligible_retry":
            first["fingerprint_valid"] = False
            error["response_observation"]["attempts"][0]["diagnostic"]["code"] = "UNSAFE_RESPONSE_FINGERPRINT"
        else:
            first["content_redacted"] = True
        for value in values:
            value["response_sha256"] = digest(value["response"])
    rewrite_bound_failure(root, transform, execution.store)
    with pytest.raises(EvidenceInvalid):
        verify_partial_intelligence_model(root, admission)


def test_unbound_native_snapshots_are_never_discovered_or_counted(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    fail_run(execution, inputs, [native(inputs, 1, "{bad")])
    first = verify_partial_intelligence_model(root, admission)
    extra_inputs = request("clean.llm_semantic", 9)
    extra = native(extra_inputs, 1)
    extra["response"]["usage"] = {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000}
    extra["response_sha256"] = digest(extra["response"])
    execution.store.snapshot(stage="clean.llm_semantic", role="output", value=extra)
    assert verify_partial_intelligence_model(root, admission) == first


@pytest.mark.parametrize("failure", ["budget", "reasoning", "fingerprint"])
def test_known_usage_survives_nonretryable_ancillary_failure(tmp_path, failure):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    value = native(inputs, 1)
    if failure == "budget":
        usage = value["response"]["usage"]
        usage["completion_tokens"] = inputs["output_ceiling"] + 1
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    elif failure == "reasoning":
        value["reasoning_state"] = "present"
    else:
        value["fingerprint_valid"] = False
    value["response_sha256"] = digest(value["response"])
    fail_run(execution, inputs, [value])
    result = verify_partial_intelligence_model(root, admission)
    assert result["known_usage"] == value["response"]["usage"]
    assert result["format_reanswers"] == 0 and result["unknown_attempts"] == 0
    assert result["business_completed"] is False


def test_source_failure_accounting_is_explicitly_outside_chat_token_report(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    stage = "search.collect"
    advance_to(execution, stage)
    with pytest.raises(RequiredModelUnavailable):
        with execution.step(stage, {}):
            execution.call_io("model.deepseek.search", {"TEST_FIXTURE": "native search request"},
                lambda: (_ for _ in ()).throw(RequiredModelUnavailable("NETWORK_TIMEOUT")))
    result = verify_partial_intelligence_model(root, admission)
    assert result["accounting_scope"] == "chat_completions_only"
    assert result["source_accounting"] == "not_certified_by_this_report"
    assert result["other_model_io_count"] == 1 and result["observed_attempts"] == []
    assert result["known_usage"] is None and result["known_native_attempts"] == 0
    assert result["observed_attempts_count"] == 0 and result["observation_state"] == "no_chat_observation"
    assert result["business_completed"] is False


def test_unknown_only_attempt_has_unknown_usage_instead_of_a_zero_cost_value(tmp_path):
    execution, root, admission = make_execution(tmp_path)
    inputs = request("clean.llm_semantic")
    fail_run(execution, inputs, [native(inputs, 1, unknown=True)])
    result = verify_partial_intelligence_model(root, admission)
    assert result["known_usage"] is None and result["known_native_attempts"] == 0
    assert result["observed_attempts_count"] == 1 and result["unknown_attempts"] == 1
    assert result["requires_reconciliation"] is True and result["observation_state"] == "usage_unknown"


def test_actual_localhost_tcp_five_stages_independent_proof_and_complete_chat_accounting(tmp_path, monkeypatch):
    """Real streamed HTTP on loopback; every business datum remains TEST_FIXTURE."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import socket
    import requests
    import main
    from test_intelligence_workflow_v2 import answer
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.paper_proof import verify_paper_execution

    native_post_json = DeepSeekRequiredClient._post_json
    native_connect = socket.socket.connect
    execution, root, args, admission, _ = business_fixture(tmp_path, monkeypatch, authoritative=True)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", native_post_json)
    wires, replies, failures = [], {}, []
    first_clean = []
    class FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                if self.path == "/v1/tools/search":
                    stage = "search.collect"
                    key = hashlib.sha256(body["query"].encode()).hexdigest()
                    response = {"results": [{"url": "https://www.reuters.com/fixture/" + key,
                        "title": "TEST_FIXTURE localhost " + key, "content": "Discovery text " + key,
                        "date": "2026-10-19T14:00:00Z"}]}
                else:
                    assert self.path == "/chat/completions"
                    content = body["messages"][1]["content"]
                    schema = json.loads(content.split("\nJSON schema:\n")[1].split("\nJSON field guidance:\n")[0])
                    payload = json.loads(content.split("\n", 1)[1].split("\nJSON schema:\n")[0])
                    properties = schema["properties"]
                    stage = ("clean.llm_semantic" if "documents" in properties else
                        "tech.sentiment" if "chains" in properties else "tech.risk" if "chain_assessments" in properties
                        else "sector.narrative" if "sectors" in properties else "decision.debate")
                    result = answer(stage, schema, payload)
                    text = json.dumps(result, ensure_ascii=False)
                    if stage == "clean.llm_semantic" and not first_clean:
                        first_clean.append(True)
                        text = "TEST_FIXTURE malformed assistant JSON from real localhost TCP"
                    response = {"id": "TEST_FIXTURE-local-" + str(len(wires) + 1),
                        "object": "chat.completion", "model": MODEL, "system_fingerprint": "fp-TEST_FIXTURE-local",
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {
                            "role": "assistant", "content": text}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
                wires.append((stage, self.path, raw))
                wire = json_bytes(response)
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    session = requests.Session()
    session.trust_env = False
    local = "http://127.0.0.1:" + str(server.server_address[1])
    loopback_connections = []
    def connect_loopback_only(sock, address):
        assert address == server.server_address
        loopback_connections.append(address)
        return native_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect_loopback_only)
    official_endpoints = []
    def loopback_post(url, **kwargs):
        assert url.startswith("https://api.deepseek.com/")
        official_endpoints.append(url)
        return session.post(local + url.removeprefix("https://api.deepseek.com"), **kwargs)
    monkeypatch.setattr(requests, "post", loopback_post)
    thread.start()
    try:
        actual = main._daily(args, "yahoo", execution=execution)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        session.close()
    assert not thread.is_alive() and not failures and loopback_connections
    result = asdict(actual.result)
    monkeypatch.setattr(requests, "post", lambda *_a, **_k: pytest.fail("Independent proof attempted HTTP"))
    summary = verify_intelligence_model(root, result, admission, response_observation_contract=CONTRACT)
    paper = verify_paper_execution(root, {**admission, "parent": None}, result, actual.sim_state)
    accounting = summary["chat_accounting"]
    chat = [row for row in wires if row[1] == "/chat/completions"]
    assert {row[0] for row in chat} == set(summary["required_stages"])
    assert chat[0][2] == chat[1][2] and len(chat) == summary["call_count"] + 1
    assert accounting["native_count"] == len(chat) and accounting["format_reanswers"] == 1
    assert accounting["known_usage"]["total_tokens"] == 200 * len(chat)
    assert accounting["unknown_attempts"] == 0 and accounting["source_accounting"] == "not_certified_by_this_report"
    assert "chat_accounting" not in result["raw"]["model"]
    assert {key: value for key, value in summary.items() if key != "chat_accounting"} == result["raw"]["model"]
    assert paper["contract"] == "tiger.paper-execution.v1" and official_endpoints
    for observation in accounting["observed_attempts"]:
        value = read_snapshot(root, observation["observation_ref"], run_id=root.name,
            stage=observation["stage"], role="output")
        transport = value["transport"]
        wire = replies[transport["body_sha256"]]
        assert transport == {"http_status": 200, "body_sha256": hashlib.sha256(wire).hexdigest(),
            "body_bytes": len(wire), "body_whitespace_only": False, "body_complete": True}
