"""Offline v2 integration across actual stages, provider parsers and model proofs.

Network boundaries are replaced explicitly; these are contract fixtures and
cannot certify a live provider's recall, financial facts or investment results.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

import main
from trading_system import pipeline
from trading_system.agent_evidence import EvidenceStore, normalize, verify_events
from trading_system.agent_runtime import policy_manifest, freeze_source
from trading_system.agents.universe_scanner_agent import UniverseScannerAgent, rank_universe
from trading_system.intelligence import default_registry
from trading_system.intelligence.transport import HttpResponse, HttpTransport
from trading_system.intelligence_workflow import (
    build_frontier, entity_registry, qualify_candidates, restore_frame,
    stage_input_packet, validate_input_packet, verify_frontier,
)
from trading_system.llm import intelligence_required as semantic
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import (
    ExecutionContext, ExecutionContractError, ExecutionPolicy, INTELLIGENCE_POLICY_ID,
    INTELLIGENCE_PROFILE, INTELLIGENCE_SEARCH_SOURCES, RequiredModelFailed,
    daily_stage_contract,
)
from trading_system.search.models import RawDocument
from trading_system.tech_chain.universe import CHAIN_TOPICS, SECTOR_TOPICS
from test_model_required_workflow import _answer as legacy_answer
from test_strict_daily_core import daily_fixture, stage_value, OfflinePublicBoundaryFixture


def answer(stage, schema, payload):
    if stage == "clean.llm_semantic":
        return {"documents": [{"id": row["id"], "tickers": [], "sentiment": "neutral",
            "sentiment_score": 0, "events": [], "summary_zh": "离线夹具：资料仅作背景，尚无可核事件。",
            "relevance": .5, "subjects": [], "evidence_spans": [], "event_records": [], "claims": []}
            for row in payload["documents"]]}
    if stage == "tech.sentiment":
        return {"chains": [{"chain_id": chain, "status": "insufficient_evidence",
            "heat": None, "sentiment_score": None, "narrative_change": None,
            "key_drivers": ["离线夹具：本链缺少通过时点与原文核验的事实。"], "evidence_ids": []}
            for chain in payload["chains"]]}
    if stage == "tech.risk":
        return {"assessment": "离线夹具：本轮缺少足够资料，不能宣称没有风险。", "alerts": [],
            "chain_assessments": [{"chain_id": chain, "status": "insufficient_evidence",
                                   "risk_level": None, "evidence_ids": []} for chain in payload["chains"]]}
    if stage == "sector.narrative":
        return {"sectors": [{"etf": etf, "status": "insufficient_evidence", "narrative_score": None,
            "eps_revision_bias": "unknown", "guidance_tone": "unknown",
            "evidence": ["离线夹具：本板块资料不足，不以中性数值代替。"], "evidence_ids": []}
            for etf in payload["etfs"]]}
    result = legacy_answer(stage, schema)
    if payload["numeric_action"] == "AVOID":
        result["action"] = "AVOID"
    return result


def intelligence_fixture(tmp_path, monkeypatch, *, failure_stage=None, corrupt=None, long_body=False,
                         authoritative=False):
    original, root, args, provider, denied = daily_fixture(tmp_path, monkeypatch)
    # Prompt replay selects only a recognized frozen implementation, exactly
    # as the real admitted worker does; a mutable checkout is not its authority.
    freeze_source(root)
    (original.paths.work_dir / "universe.txt").write_text("AAPL\nMU\nNVDA\n")
    allowed = {"AAPL", "MU", "NVDA"}
    from trading_system.markets import get_market
    allowed.update(get_market("us").sector_symbols)
    monkeypatch.setattr(provider, "ohlcv_batch", lambda symbols, days=400:
        {symbol: provider.ohlcv(symbol, days) for symbol in symbols if symbol in allowed})
    execution = ExecutionContext(run_id=root.name, run_date=original.run_date,
        paths=original.paths, policy=ExecutionPolicy(INTELLIGENCE_POLICY_ID, model_mode="enabled",
            provider_names=("yahoo",), search_source_names=INTELLIGENCE_SEARCH_SOURCES),
        store=EvidenceStore(root, root.name), admitted_at=datetime(2026, 10, 20, 22, tzinfo=timezone.utc),
        authoritative_paper=authoritative)
    if authoritative:
        before = root / "account-before"
        before.mkdir()
        for name in ("journal.json", "sim_portfolio.json"):
            (before / name).write_bytes((execution.paths.reports_dir / name).read_bytes())
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "fixture-secret-only"}.items():
        monkeypatch.setenv(key, value)
    config = {"sources": {source: {"enabled": source in {"web_page", "deepseek"},
        "minimum_interval_seconds": 0, "max_retries": 0} for source in default_registry().source_ids(enabled_only=False)}}
    config_path = tmp_path / "trusted-registry.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("TIGER_INTEL_REGISTRY_FILE", str(config_path))
    monkeypatch.delenv("TIGER_INTEL_ARCHIVE_ROOT", raising=False)
    stage_input_packet(root, as_of=execution.admitted_at)
    calls = []
    def post(client, path, request):
        stage = client.execution._active.spec.name
        calls.append((stage, path))
        if stage == failure_stage:
            raise LLMUnavailable("offline fixture provider failure")
        if path == "/v1/tools/search":
            key = hashlib.sha256(request["query"].encode()).hexdigest()
            response = {"results": [{"url": "https://www.reuters.com/fixture/" + key,
                "title": "TEST_FIXTURE " + key, "content": "Discovery text " + key,
                "date": "2026-10-19T14:00:00Z"}]}
        else:
            content = request["messages"][1]["content"]
            schema = json.loads(content.split("\nJSON schema:\n")[1].split("\nJSON field guidance:\n")[0])
            payload = json.loads(content.split("\n", 1)[1].split("\nJSON schema:\n")[0])
            result = answer(stage, schema, payload)
            if corrupt:
                result = corrupt(stage, result)
            response = {"id": "fixture-" + uuid.uuid4().hex, "model": "deepseek-flash",
                "object": "chat.completion", "system_fingerprint": "fp-offline-fixture",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
        return response, hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    def fetch(transport, method, url, **kwargs):
        assert method == "GET" and url.startswith("https://www.reuters.com/fixture/")
        text = (("Full source paragraph. " * 400) if long_body else "Original source article. ")
        text += "TAIL " + url.rsplit("/", 1)[-1]
        body = ('<html><head><title>TEST_FIXTURE original</title>'
                '<meta property="article:published_time" content="2026-10-19T14:00:00Z">'
                '</head><body><article>' + text + '</article></body></html>').encode()
        return HttpResponse(url, 200, {"content-type": "text/html"}, body,
                            "2026-10-20T22:01:00+00:00", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(HttpTransport, "request", fetch)
    return execution, root, args, provider, denied, calls


def test_v2_policy_reorders_market_and_freezes_before_any_score():
    policy = policy_manifest(INTELLIGENCE_PROFILE)
    stages = [row["name"] for row in policy["stages"]]
    assert len(stages) == 41
    assert stages.index("data.prepare") < stages.index("search.collect")
    assert stages.index("intelligence.frontier") < stages.index("search.collect")
    assert stages.index("clean.cross_validate") < stages.index("intelligence.freeze") < stages.index("tech.sentiment")
    assert policy["model_contract"]["search_mode"] == "intelligence_source_registry"
    assert policy["strategy"] == "reviewed_source_defaults" and "sector_proxy_snapshot" not in policy


def test_full41_stage_partial_research_keeps_nulls_and_frozen_originals(tmp_path, monkeypatch):
    execution, root, args, _, denied, calls = intelligence_fixture(tmp_path, monkeypatch, long_body=True)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    events = verify_events(root, execution.run_id, daily_stage_contract(True, intelligence=True))
    assert events["stage_count"] == 41 and not events["degraded"]
    result = output.result
    intelligence = result.raw["intelligence"]
    assert intelligence["state"] == "partial" and intelligence["bundle"]["missing_required"]
    assert result.picks == [] and result.action in {"WAIT", "AVOID"}
    assert result.raw["model"]["call_count"] >= 5
    markdown = main.render_markdown(result)
    assert "模型不可用或回复不合格会阻止本轮完成" in markdown
    assert "红线透传" not in markdown
    assert "每条事实只在自己的允许用途和目标内使用" in markdown
    assert "尚未取得能回答此问题的合格原文事实" in markdown
    assert "no_qualified_material_for_this_question" not in markdown
    assert {stage for stage, path in calls if path == "/chat/completions"} == {
        "clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"}
    assert not any(stage == "decision.debate" and path != "/chat/completions" for stage, path in calls)
    narrative = stage_value(execution, root, "sector.narrative")["raw_result"]
    assert all(row["score"] is None for row in narrative.values())
    assert all(signal["fields"]["risk_level"] is None for signal in stage_value(execution, root, "tech.fusion"))
    cleaned = stage_value(execution, root, "clean.llm_semantic")["documents"]
    bodies = [row["fields"]["raw"]["fields"]["content"] for row in cleaned]
    assert any(len(body) > 4000 and "TAIL " in body for body in bodies)
    assert all(row["fields"]["annotation_contract"] == "typed-evidence/v2" for row in cleaned)
    bundle = stage_value(execution, root, "intelligence.freeze")
    assert bundle["frontier_manifest_hash"] == intelligence["frontier"]["manifest_sha256"]
    assert len(json.dumps(intelligence)) < 150_000  # public summary contains references, not full originals
    assert denied == []


@pytest.mark.parametrize("stage", ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"])
def test_v2_model_failure_never_becomes_passthrough_or_a_completed_run(tmp_path, monkeypatch, stage):
    execution, root, args, _, denied, _ = intelligence_fixture(tmp_path, monkeypatch, failure_stage=stage)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == stage and execution.summary()[-1]["status"] == "failed"
    assert not (root / "receipt.json").exists() and denied == []


def test_clean_schema_injected_client_cannot_invent_unqualified_chain_score(tmp_path, monkeypatch):
    def corrupt(stage, output):
        if stage == "tech.sentiment":
            output["chains"][0].update(status="assessed", heat=9, sentiment_score=.9, narrative_change="improving")
        return output
    execution, _, args, _, _, _ = intelligence_fixture(tmp_path, monkeypatch, corrupt=corrupt)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)


def market():
    provider = OfflinePublicBoundaryFixture(datetime(2026, 10, 20).date())
    return {"spy": provider.ohlcv("SPY"), "stock_ohlcv": {
        symbol: provider.ohlcv(symbol) for symbol in ("MU", "NVDA", "AAPL")}}


def test_frontier_uses_same_pure_rank_and_does_not_call_entry_setup(monkeypatch):
    data = market()
    universe = ["MU", "NVDA", "AAPL"]
    ranks, _, _ = rank_universe(universe, data)
    monkeypatch.setattr("trading_system.agents.tss_agent.TSSAgent.entry_template_for",
                        lambda *args: pytest.fail("Frontier must not evaluate a semantic-dependent final entry"))
    frontier = build_frontier(data, universe, {symbol: "listed:" + symbol for symbol in universe},
        as_of=datetime.now(timezone.utc), market_snapshot_ref={"sha256": "f" * 64}, top_n=1)
    assert frontier["ranked"] == ranks and frontier["symbols"] == [ranks[0]["ticker"]]


def test_frontier_holdings_missing_sector_hash_and_tamper_are_explicit():
    data = market()
    universe = ["MU", "NVDA", "AAPL"]
    ids = {symbol: "listed:" + symbol for symbol in universe}
    as_of = datetime(2026, 10, 20, 22, tzinfo=timezone.utc)
    options = {"as_of": as_of, "market_snapshot_ref": {"sha256": "f" * 64},
               "portfolio_state": {"positions": [{"ticker": "AAPL"}, {"ticker": "UNKNOWN"}], "pending": []},
               "sector_symbols": ["XLE"]}
    frontier = build_frontier(data, universe, ids, **options)
    assert frontier["holdings"] == ["AAPL", "UNKNOWN"]
    assert frontier["selection_reasons"]["AAPL"][0] == "admitted_holding_or_pending"
    assert frontier["missing"][0]["symbol"] == "UNKNOWN" and frontier["sector_gaps"][0]["sector"] == "XLE"
    verify_frontier(frontier, data, universe, **options)
    altered = copy.deepcopy(frontier)
    altered["symbols"].pop()
    with pytest.raises(ExecutionContractError):
        verify_frontier(altered, data, universe, **options)


def test_unresearched_final_scan_supplement_cannot_bypass_frozen_frontier():
    context = {"watchlist": [SimpleNamespace(ticker="AAPL"), SimpleNamespace(ticker="MU")], "scan_stats": {}}
    gaps = qualify_candidates(context, {"symbols": ["MU"]})
    assert [row.ticker for row in context["watchlist"]] == ["MU"]
    assert gaps == [{"symbol": "AAPL", "reason": "outside_frozen_research_frontier",
                     "next_action": "new_evidence_version_and_rescore"}]


def test_market_frame_replay_is_lossless_and_rejects_extra_or_altered_types():
    frame = market()["spy"]
    assert normalize(restore_frame(normalize(frame))) == normalize(frame)
    broken = normalize(frame)
    broken["values"][0][0] = True
    with pytest.raises(ExecutionContractError):
        restore_frame(broken)


def test_frozen_v2_source_contains_new_module_and_input_configuration_cannot_self_register(tmp_path):
    root = tmp_path / "frozen"
    root.mkdir()
    source = freeze_source(root)
    assert {"trading_system/intelligence/service.py", "trading_system/llm/intelligence_required.py"} <= {
        item["path"] for item in source["files"]}
    registry = default_registry()
    packet = {"schema": "tiger.intelligence-input.v2", "config": {}, "registry": registry.public_manifest(), "baseline": None}
    validate_input_packet(packet)
    packet["registry"]["sources"]["brave"]["enabled"] = True
    with pytest.raises(Exception, match="configuration differs"):
        validate_input_packet(packet)


def test_every_actual_scored_sector_has_a_dedicated_query_target():
    topics = semantic.acquisition_topics({**CHAIN_TOPICS, **SECTOR_TOPICS})
    assert set(topics) == set(semantic.TARGET_IDS)
    assert topics["XLE"] == SECTOR_TOPICS["energy"]


def test_invalid_trusted_registry_returns_durable_failure_with_the_original_identity(tmp_path, monkeypatch):
    from trading_system.agent_runner import submit
    path = tmp_path / "invalid-registry.json"
    path.write_text('{"sources":{"unregistered_external_source":{"enabled":true}}}')
    monkeypatch.setenv("TIGER_INTEL_REGISTRY_FILE", str(path))
    monkeypatch.delenv("TIGER_INTEL_ARCHIVE_ROOT", raising=False)
    request = {"schema_version": "tiger.agent-request/v1", "profile": INTELLIGENCE_PROFILE,
               "paper_account": "public-usd", "idempotency_key": "invalid-registry-fixture"}
    state = tmp_path / "state"
    failed = submit(state, request)
    assert failed["operation_ok"] is False and failed["error"]["code"] == "INVALID_REQUEST"
    assert failed["run_id"]
    repeated = submit(state, request)
    assert repeated["run_id"] == failed["run_id"]
    assert not list(state.glob("runs/*/processes.json"))


def test_authoritative_v2_paper_ledger_replays_without_model_or_network_during_verify(tmp_path, monkeypatch):
    from trading_system.agent_runtime import frozen_clock
    from trading_system.paper_proof import verify_paper_execution
    execution, root, args, _, denied, _ = intelligence_fixture(tmp_path, monkeypatch, authoritative=True)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    verify_events(root, execution.run_id, daily_stage_contract(True, authoritative_paper=True, intelligence=True))
    admission = {"clock": frozen_clock(execution.admitted_at), "parent": None}
    proof = verify_paper_execution(root, admission, asdict(output.result), output.sim_state)
    assert proof["contract"] == "tiger.paper-execution.v1"
    assert denied == [] and output.result.raw["paper_execution_version"] == "tiger.paper-execution.v1"


def official_document(*, published="2026-11-02T14:30:00Z", relation="supports"):
    from trading_system.search.event_identity import validate_structured_events
    from test_structured_evidence import _doc, _policy, _authority, REGISTRY, CUTOFF
    document = _doc(host="www.sec.gov")
    document.raw = replace(document.raw,
        url="https://www.sec.gov/Archives/edgar/data/123/000000012326000001/alpha.htm",
        published=published, meta={**document.raw.meta, "source_times": {
            "published": {"value": published, "precision": "instant"}}})
    document.event_records[0]["claim_kind"] = "issuer_disclosure"
    document.claims[0]["relation"] = relation
    _authority(document, "sec", "sec_filing", issuer="issuer:0000000123",
        proof_type="immutable_accession", accession="0000000123-26-000001",
        accepted_at=published)
    policy = _policy("sec", "www.sec.gov", "sec_filing")
    validate_structured_events([document], CUTOFF, entity_registry=REGISTRY, source_policies=policy)
    return document, CUTOFF


def test_owned_frozen_action_evidence_is_not_blocked_by_an_unrelated_sector():
    document, cutoff = official_document()
    assert document.eligibility["events"][0]["action_security_ids"] == ["listed:alpha"]
    bundle = {"gaps": [{"cell_id": "unrelated", "target_id": "sector:XLE", "required": True, "status": "missing"}]}
    outcomes, sources = semantic.company_evidence_from_frozen([document], ["AAA"], cutoff,
        security_ids={"AAA": "listed:alpha"}, bundle=bundle)
    assert outcomes["AAA"]["source_ids"] == [document.raw.doc_id] and sources
    assert outcomes["AAA"]["required_targets"] == ["global:macro", "security:listed:alpha"]
    bundle["gaps"].append({"cell_id": "company", "target_id": "security:listed:alpha", "required": True, "status": "missing"})
    outcomes, sources = semantic.company_evidence_from_frozen([document], ["AAA"], cutoff,
        security_ids={"AAA": "listed:alpha"}, bundle=bundle)
    assert outcomes["AAA"]["status"] == "required_coverage_missing" and not sources


def test_frozen_company_evidence_never_uses_other_security_or_changed_source():
    document, cutoff = official_document()
    outcomes, _ = semantic.company_evidence_from_frozen([document], ["BBB"], cutoff,
        security_ids={"BBB": "listed:beta"}, bundle={"gaps": []})
    assert outcomes["BBB"]["source_ids"] == []
    document.raw = replace(document.raw, content=document.raw.content + " Changed after validation.")
    outcomes, _ = semantic.company_evidence_from_frozen([document], ["AAA"], cutoff,
        security_ids={"AAA": "listed:alpha"}, bundle={"gaps": []})
    assert outcomes["AAA"]["source_ids"] == []


def test_current_financial_fact_does_not_silently_close_adverse_or_counterevidence_questions():
    document, cutoff = official_document()
    plan = {"as_of": cutoff.isoformat(), "coverage_cells": [{"cell_id": material,
        "target_id": "security:listed:alpha", "material_type": material, "window_days": 450}
        for material in ("issuer_financial", "adverse_event", "counterevidence")]}
    assessment = semantic.assessment_rows([document], plan)
    assert [row["status"] for row in assessment["coverage"]] == ["present", "insufficient_evidence", "insufficient_evidence"]
    assert assessment["coverage"][0]["document_ids"] == [document.raw.meta["intelligence_document_id"]]


def test_declared_financial_baseline_can_fill_baseline_without_becoming_current_catalyst():
    document, cutoff = official_document(published="2026-06-02T14:30:00Z")
    assert document.eligibility["events"][0]["status"] == "historical_baseline"
    plan = {"as_of": cutoff.isoformat(), "coverage_cells": [{"cell_id": "baseline", "target_id": "security:listed:alpha",
        "material_type": "issuer_financial", "window_days": 450}]}
    rows = semantic.assessment_rows([document], plan)["coverage"]
    assert rows[0]["status"] == "present"
    outcomes, _ = semantic.company_evidence_from_frozen([document], ["AAA"], cutoff,
        security_ids={"AAA": "listed:alpha"}, bundle={"gaps": []})
    assert outcomes["AAA"]["source_ids"] == []
    plan["coverage_cells"][0]["window_days"] = 90
    assert semantic.assessment_rows([document], plan)["coverage"][0]["status"] == "insufficient_evidence"


@pytest.mark.parametrize("account_field,unavailable_field,target,reason", [
    ("holdings", "unavailable_holdings", "account:holding:ZZZ", "HOLDING_OUTSIDE_REVIEWED_ROSTER"),
    ("pending", "unavailable_pending", "account:pending:ZZZ", "PENDING_OUTSIDE_REVIEWED_ROSTER"),
])
def test_unavailable_connected_account_security_stays_required_without_expanding_trade_scope(
        account_field, unavailable_field, target, reason):
    from trading_system.intelligence import build_plan
    document, cutoff = official_document()
    plan = build_plan({"run_id": "fixture", "as_of": cutoff.isoformat(), "market": "us",
        "universe": {"AAA": "listed:alpha"}, "frontier": ["AAA"], account_field: ["ZZZ"],
        "frontier_manifest_hash": "a" * 64, "topics": {}}, default_registry())
    assert plan[unavailable_field] == ["ZZZ"] and plan["allowed_entities"] == ["listed:alpha"]
    assessment = semantic.assessment_rows([document], plan)
    row = next(row for row in assessment["coverage"] if row["target_id"] == target)
    assert row["status"] == "unverifiable" and row["reason"] == reason
    bundle = {"gaps": [{**row, "required": True}]}
    outcomes, sources = semantic.company_evidence_from_frozen([document], ["AAA"], cutoff,
        security_ids={"AAA": "listed:alpha"}, bundle=bundle)
    assert outcomes["AAA"]["status"] == "required_coverage_missing" and not sources


@pytest.mark.parametrize("relation,status", [("supports", "present"), ("refutes", "present"),
                                              ("mentions", "insufficient_evidence")])
def test_historical_financial_baseline_requires_an_asserted_fact_not_a_mention(relation, status):
    document, cutoff = official_document(published="2026-06-02T14:30:00Z", relation=relation)
    assert document.eligibility["events"][0]["status"] == "historical_baseline"
    plan = {"as_of": cutoff.isoformat(), "coverage_cells": [{"cell_id": "baseline",
        "target_id": "security:listed:alpha", "material_type": "issuer_financial", "window_days": 450}]}
    row = semantic.assessment_rows([document], plan)["coverage"][0]
    assert row["status"] == status
    assert bool(row["claim_ids"]) == (status == "present")


def test_v2_actual_submitted_worker_without_model_key_fails_before_external_io(tmp_path, monkeypatch):
    from agent_workflow_fixtures import wait_terminal
    from trading_system.agent_runner import submit
    from trading_system.agent_runtime import pointer_read

    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash"}.items():
        monkeypatch.setenv(key, value)
    for key in ("LLM_API_KEY", "TIGER_INTEL_REGISTRY_FILE", "TIGER_INTEL_ARCHIVE_ROOT"):
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / "state"
    request = {"schema_version": "tiger.agent-request/v1", "profile": INTELLIGENCE_PROFILE,
               "paper_account": "public-usd", "idempotency_key": "no-model-key-fixture"}
    accepted = submit(state, request)
    assert accepted["operation_ok"] and accepted["status"] == "accepted"
    observed = wait_terminal(state, accepted["run_id"])
    assert observed["status"] == "failed" and observed["error"]["code"] == "MODEL_REQUIRED_FAILED"
    run = state / "runs" / accepted["run_id"]
    assert (run / "source/trading_system/intelligence/service.py").is_file()
    assert not (run / "receipt.json").exists() and pointer_read(state) is None
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    assert [event["stage"] for event in events if event["kind"] == "begin"] == [
        "daily.preflight", "state.reset", "pipeline.prepare"]
    assert events[-1]["stage"] == "pipeline.prepare" and events[-1]["status"] == "failed"
    stage_refs = {event[key]["path"] for event in events for key in ("input_ref", "output_ref")
                  if event.get(key)}
    for event in events:
        if event["kind"] == "end" and event.get("output_ref"):
            calls = json.loads((run / event["output_ref"]["path"]).read_text())["value"]["io_calls"]
            assert all(call["method"] in {"file.read_json", "file.read", "state.purge_run_state"} for call in calls)
            stage_refs.update(call[key]["path"] for call in calls
                              for key in ("input_ref", "output_ref", "error_ref") if call.get(key))
    assert {path.relative_to(run).as_posix() for path in (run / "evidence").glob("*.json")} == stage_refs
    replay = submit(state, request)
    assert replay["run_id"] == accepted["run_id"] and replay["data"]["idempotent_replay"] is True
