"""Offline required-model integration and rejection cases, not live research."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

import main
from trading_system.agent_evidence import (EvidenceInvalid, EvidenceStore, verify_events,
    atomic_json, atomic_bytes, read_json, read_bytes, file_ref, digest, json_bytes, SNAPSHOT_SCHEMA)
from trading_system.agent_runtime import policy_manifest, PROFILE, ACTIVE_PROFILE, run_environment
from trading_system.llm import model_budget, required
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.required_proof import verify_required_model
from trading_system.redline import LLMUnavailable, ExecutionTracer, llm_guard
from trading_system.redline.execution import (ExecutionContext, ExecutionPolicy,
    RequiredModelFailed, REQUIRED_POLICY_ID, REQUIRED_PROFILE, REQUIRED_MODEL_STAGES,
    daily_stage_contract, daily_stage_specs)
from trading_system.search.credibility import CrossValidator
from trading_system.search.models import RawDocument, CleanDocument
from test_strict_daily_core import daily_fixture, stage_value


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _admission(execution):
    return {"clock": {"run_date": execution.run_date.isoformat(),
                      "market_timezone": execution.policy.market_timezone}}


def _answer(stage, schema):
    props = schema["properties"]
    if stage == "clean.llm_semantic":
        ids = props["documents"]["items"]["properties"]["id"]["enum"]
        return {"documents": [{"id": item, "tickers": [], "sentiment": "neutral",
            "sentiment_score": 0, "events": [], "summary_zh": "测试夹具：公开资料尚不足以支持交易。",
            "relevance": .5} for item in ids]}
    if stage == "tech.sentiment":
        props = props["chains"]["items"]["properties"]
        return {"chains": [{"chain_id": item, "heat": 5, "sentiment_score": 0,
            "narrative_change": "stable", "key_drivers": ["测试夹具：仅作契约验证"],
            "evidence_ids": props["evidence_ids"]["items"]["enum"][:1]}
            for item in props["chain_id"]["enum"]]}
    if stage == "tech.risk":
        return {"assessment": "测试夹具：所有链均已评估，没有有据风险；不代表真实研究。", "alerts": []}
    if stage == "sector.narrative":
        props = props["sectors"]["items"]["properties"]
        return {"sectors": [{"etf": item, "narrative_score": 5,
            "eps_revision_bias": "unknown", "guidance_tone": "unknown",
            "evidence": ["测试夹具：未作实际研究"],
            "evidence_ids": props["evidence_ids"]["items"]["enum"][:1]}
            for item in props["etf"]["enum"]]}
    props = props["decisions"]["items"]["properties"]
    return {"action": "WAIT", "action_summary": "测试夹具：等待，不构成真实交易研究。",
        "decisions": [{"symbol": symbol, "action": "watch", "action_summary": "继续观察",
            "why_this": "测试候选", "why_now": "当前缺少批准买入条件", "entry_condition": "全部风控和证据通过后重新研究",
            "invalidation": "证据或价格失效即放弃", "risk_note": "测试结果没有实际交易含义",
            "source_ids": [item for item in props["source_ids"]["items"]["enum"]
                           if item.startswith("md_" + symbol + "_")]}
            for symbol in props["symbol"].get("enum", [])]}


def required_fixture(tmp_path, monkeypatch, *, failure_stage=None, corrupt_answer=None,
                     article_text=None, captured_requests=None):
    original, root, args, fixture, denied = daily_fixture(tmp_path, monkeypatch)
    execution = ExecutionContext(run_id=root.name, run_date=original.run_date,
        paths=original.paths, policy=ExecutionPolicy(REQUIRED_POLICY_ID, model_mode="enabled",
            provider_names=("yahoo",), search_source_names=("deepseek_search",)),
        store=EvidenceStore(root, root.name))
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "fixture-secret-only"}.items():
        monkeypatch.setenv(key, value)
    attempts = []
    def post(client, path, request):
        stage = client.execution._active.spec.name
        attempts.append((stage, path))
        if stage == failure_stage:
            raise LLMUnavailable("fixture provider unavailable")
        if path == "/v1/tools/search":
            fingerprint = hashlib.sha256(request["query"].encode()).hexdigest()
            body = article_text(fingerprint) if article_text else "TEST_FIXTURE Public source content " + fingerprint
            payload = {"results": [{"url": "https://www.reuters.com/fixture/" + fingerprint,
                "title": "TEST_FIXTURE " + fingerprint, "content": body,
                "date": "2026-10-20T12:00:00Z"}]}
        else:
            if captured_requests is not None:
                captured_requests.append((stage, copy.deepcopy(request)))
            schema = json.loads(request["messages"][1]["content"].split("\nJSON schema:\n")[1].split("\nJSON field guidance:\n")[0])
            answer = _answer(stage, schema)
            if corrupt_answer:
                answer = corrupt_answer(stage, answer)
            payload = {"id": "fixture-" + uuid.uuid4().hex, "model": "deepseek-flash",
                "object": "chat.completion", "system_fingerprint": "fp-fixture",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
        return payload, _hash(payload)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    return execution, root, args, fixture, denied, attempts


def test_all39_stages_require_model_proof_and_model_plan(tmp_path, monkeypatch):
    execution, root, args, _, denied, attempts = required_fixture(tmp_path, monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    verified = verify_events(root, execution.run_id, daily_stage_contract(True))
    assert verified["stage_count"] == 39 and not verified["degraded"]
    assert all(row["status"] == "completed" for row in execution.summary())
    assert stage_value(execution, root, "report.plan")["enabled"] is True
    result = asdict(output.result)
    proof = verify_required_model(root, result, _admission(execution))
    assert proof == result["raw"]["model"]
    assert proof["search_count"] == 14 and proof["call_count"] == 5
    assert result["action"] == "WAIT" and result["picks"] == []
    assert result["raw"]["activity_profile"] is None
    scan = result["raw"]["scan_stats"]
    coverage = scan["entry_setup_supplement"]
    assert coverage["checked"] == scan["passed"] - coverage["baseline_selected"]
    assert scan["selected"] == coverage["baseline_selected"] + coverage["added"]
    assert {stage for stage, path in attempts if path == "/chat/completions"} == set(REQUIRED_MODEL_STAGES)
    assert denied == []


@pytest.mark.parametrize("small_window", [False, True])
def test_native_source_tail_and_lineage_survive_real_request_planning(tmp_path, monkeypatch, small_window):
    if small_window:
        monkeypatch.setattr(model_budget, "CONTEXT_TOKENS", 25_000)

    def article(identity):
        prefix = "公司披露经营数据及供应情况。" * 640
        return prefix + '\n实际原文尾部 "https://example.com/a?q=1" ' + identity

    sent = []
    execution, root, args, _, denied, _ = required_fixture(
        tmp_path, monkeypatch, article_text=article, captured_requests=sent)
    output = main._daily(args, "yahoo", execution=execution)
    proof = verify_required_model(root, asdict(output.result), _admission(execution))
    assert proof == output.result.raw["model"]
    assert denied == []

    cleaning_inputs = []
    for stage, body in sent:
        user = body["messages"][1]["content"]
        payload = json.loads(user.split("\n", 1)[1].split("\nJSON schema:\n", 1)[0])
        assert body["max_tokens"] == min(model_budget.MAX_OUTPUT_TOKENS,
            model_budget.CONTEXT_TOKENS - model_budget.count_json_tokens(body))
        if stage == "clean.llm_semantic":
            cleaning_inputs.append(payload["documents"])
        elif small_window:
            assert payload["document_scope"] == required.SOURCE_SUMMARY_PROJECTION
            assert all("content" not in item and item["summary_zh"] for item in payload["documents"])
        else:
            assert all("实际原文尾部" in item["content"] for item in payload["documents"])

    documents = [item for group in cleaning_inputs for item in group]
    assert len(documents) == len({item["id"] for item in documents}) == 14
    assert (len(cleaning_inputs) > 1) is small_window
    by_id = {}
    for item in documents:
        body = article(item["url"].rsplit("/", 1)[1])
        assert len(body) > 4000 and item["content"] == body
        assert item["native_content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
        by_id[item["id"]] = item["native_content_sha256"]
    for stage, body in sent:
        if stage != "clean.llm_semantic":
            user = body["messages"][1]["content"]
            payload = json.loads(user.split("\n", 1)[1].split("\nJSON schema:\n", 1)[0])
            assert all(by_id[item["id"]] == item["native_content_sha256"] for item in payload["documents"])


@pytest.mark.parametrize("stage", ["search.collect", *REQUIRED_MODEL_STAGES])
def test_model_or_search_failure_stops_whole_run(tmp_path, monkeypatch, stage):
    execution, root, args, _, denied, _ = required_fixture(tmp_path, monkeypatch, failure_stage=stage)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == stage
    assert execution.summary()[-1]["status"] == "failed"
    assert all(row["status"] != "passthrough" for row in execution.summary())
    assert not (root / "receipt.json").exists() and not (root / "current.json").exists()
    assert denied == []


@pytest.mark.parametrize("tamper", ["model", "summary", "instruction", "action", "pick"])
def test_independent_proof_rejects_unbound_summary_and_instructions(tmp_path, monkeypatch, tamper):
    execution, root, args, _, _, _ = required_fixture(tmp_path, monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    result = asdict(output.result)
    if tamper == "model":
        result["raw"]["model"]["response_ids"][0] = "invented-id"
    elif tamper == "summary":
        result["raw"]["action_summary"] = "Unbound rules summary"
    elif tamper == "instruction":
        # Empty watchlists still cannot manufacture a model instruction.
        result["raw"]["execution_decisions"] = [{"symbol": "ZZZ", "action": "buy"}]
    elif tamper == "action":
        result["action"] = "BUY"
    else:
        result["picks"] = [{"ticker": "ZZZ", "shares": 100}]
    with pytest.raises(EvidenceInvalid) as exc:
        verify_required_model(root, result, _admission(execution))
    assert exc.value.code == "MODEL_REQUIRED_FAILED"


def test_required_guard_does_not_turn_none_or_transport_error_into_passthrough(tmp_path, monkeypatch):
    execution, _, _, _, _, _ = required_fixture(tmp_path, monkeypatch)
    tracer = ExecutionTracer(execution=execution)
    with pytest.raises(RequiredModelFailed):
        llm_guard("tech.risk", lambda: None, [], tracer)
    with pytest.raises(RequiredModelFailed):
        llm_guard("tech.risk", lambda: (_ for _ in ()).throw(LLMUnavailable("fixture")), [], tracer)


def test_partial_semantic_response_is_failure(tmp_path, monkeypatch):
    def corrupt(stage, answer):
        if stage == "clean.llm_semantic":
            answer["documents"].pop()
        return answer
    execution, _, args, _, _, _ = required_fixture(tmp_path, monkeypatch, corrupt_answer=corrupt)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == "clean.llm_semantic"


def _rebind_stage(root, stage, *, prompt=False):
    """Rehash deliberate corruption to exercise semantic checks beyond hashes."""
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == stage and row["kind"] == "end")
    old_ref = end["output_ref"]
    blob = read_json(root, old_ref["path"])
    output = blob["value"]
    if prompt:
        call = output["io_calls"][0]
        request_blob = read_json(root, call["input_ref"]["path"])
        request = request_blob["value"]
        request["request"]["messages"][1]["content"] = request["request"]["messages"][1]["content"].replace(
            '"documents":[', '"documents_omitted":[', 1)
        request["request_sha256"] = _hash(request["request"])
        atomic_json(root, call["input_ref"]["path"], request_blob)
        call["input_ref"] = file_ref(root, call["input_ref"]["path"], SNAPSHOT_SCHEMA)
        proof_blob = read_json(root, call["output_ref"]["path"])
        proof_blob["value"]["request_sha256"] = request["request_sha256"]
        atomic_json(root, call["output_ref"]["path"], proof_blob)
        call["output_ref"] = file_ref(root, call["output_ref"]["path"], SNAPSHOT_SCHEMA)
    else:
        value = output["value"]
        if stage == "clean.llm_semantic":
            value["documents"][0]["fields"]["summary_zh"] = "规则伪造摘要"
        elif stage == "tech.sentiment":
            value[next(iter(value))]["fields"]["heat"] = 9
        elif stage == "tech.risk":
            value.append({"severity": 9, "headline_zh": "规则伪造风险"})
        else:
            value["narrative"][next(iter(value["narrative"]))]["score"] = 9
    atomic_json(root, old_ref["path"], blob)
    new_ref = file_ref(root, old_ref["path"], SNAPSHOT_SCHEMA)
    previous = None
    for row in events:
        for key in ("input_ref", "output_ref", "previous_stage_ref"):
            if row[key] == old_ref:
                row[key] = new_ref
        row["previous_event_sha256"] = previous
        row["event_sha256"] = digest(json_bytes({key: val for key, val in row.items() if key != "event_sha256"}))
        previous = row["event_sha256"]
    atomic_bytes(root, "events.jsonl", b"".join(json_bytes(row) for row in events))


@pytest.mark.parametrize("stage", ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative"])
@pytest.mark.parametrize("prompt", [False, True])
def test_valid_model_call_does_not_authorize_replaced_semantic_output_or_missing_documents(tmp_path, monkeypatch, stage, prompt):
    execution, root, args, _, _, _ = required_fixture(tmp_path, monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    _rebind_stage(root, stage, prompt=prompt)
    # All byte hashes and event links remain valid; model semantics still fail.
    verify_events(root, execution.run_id, daily_stage_contract(True))
    with pytest.raises(EvidenceInvalid) as exc:
        verify_required_model(root, asdict(output.result), _admission(execution))
    assert exc.value.code == "MODEL_REQUIRED_FAILED"


def test_required_policy_preserves_old_profile_serialization_and_denies_passthrough():
    for profile in (PROFILE, ACTIVE_PROFILE):
        policy = policy_manifest(profile)
        assert policy["execution"]["model_mode"] == "disabled"
        assert "model_contract" not in policy
        assert all(row["allow_passthrough"] for row in policy["stages"] if row["driver"] == "llm")
    policy = policy_manifest(REQUIRED_PROFILE)
    assert policy["strategy"] == "reviewed_source_defaults"
    assert "sector_proxy_snapshot" not in policy
    assert policy["model_contract"]["thinking_mode"] == "scene-policy"
    assert policy["model_contract"]["thinking_policy"]["clean.llm_semantic"] == {"type": "disabled"}
    assert policy["model_contract"]["thinking_policy"]["decision.debate"] == {
        "type": "enabled", "effort": "max"}
    assert policy["execution"]["search_source_names"] == ("deepseek_search",)
    assert all(not row["allow_passthrough"] for row in policy["stages"])
    assert len(policy["stages"]) == 39
    with pytest.raises(Exception):
        ExecutionPolicy("unreviewed-policy", model_mode="enabled")


def test_only_trusted_required_environment_carries_model_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fixture-only")
    monkeypatch.setenv("TIGER_PNL_API_TOKEN", "forbidden-fixture")
    assert "LLM_API_KEY" not in run_environment(tmp_path)
    env = run_environment(tmp_path, model_required=True)
    assert env["LLM_API_KEY"] == "fixture-only"
    assert "TIGER_PNL_API_TOKEN" not in env


def test_native_search_corroboration_counts_original_publishers_without_relaxing_thresholds():
    def doc(identity, url):
        return CleanDocument(RawDocument(identity, "deepseek_search", "Same event", url,
            "A public factual event description for a corroboration test.", "2026-10-20"),
            tickers=["AAA"], events=["earnings"], degraded=False)
    one = [doc("a", "https://www.reuters.com/a"), doc("b", "https://www.reuters.com/b")]
    assert CrossValidator().validate(one)["corroborated"] == 0
    two = [doc("a", "https://www.reuters.com/a"), doc("b", "https://www.sec.gov/b")]
    assert CrossValidator().validate(two)["corroborated"] == 2
