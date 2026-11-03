"""Company evidence coverage and veto through the real offline 39-stage workflow."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json

import pytest

import main
from trading_system import pipeline
from trading_system.agent_evidence import EvidenceInvalid, read_bytes, read_json, verify_events
from trading_system.data_models import TradePick
from trading_system.llm.company_evidence import COMPANY_EVIDENCE_CONTRACT, candidate_searches
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.llm.required_proof import verify_required_model
from trading_system.market_close import verify_close_evidence
from trading_system.redline.execution import RequiredModelFailed, daily_stage_contract
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_completed_market_close import _with_clock
from test_model_required_temporal import _rewrite_events, _save_snapshot
from test_model_required_workflow import _admission, _hash, required_fixture

URL = "https://www.sec.gov/Archives/edgar/data/123/000000012326000001/issuer-8k.htm"


def _filing(symbol="AAA", *, published="2026-10-20T12:00:00Z", url=URL):
    return {
        "url": url, "title": "Fixture issuer filing", "date": published,
        "content": (
            "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\nFORM 8-K\n"
            "| Title of each class | | Trading Symbol(s) | | Name of each exchange on which registered |\n"
            "| --- | --- | --- | --- | --- |\n"
            f"| Class A Common Stock | | {symbol} | | New York Stock Exchange |\n\n"
            "Fixture disclosure mentions AAA elsewhere; the cover determines its issuer."
        ),
    }


def _fixture(tmp_path, monkeypatch, *, source="valid", model_buy=False, aligned_close=False):
    candidate_requests = []
    observed = {}

    def model_answer(stage, output):
        if stage == "decision.debate" and model_buy:
            output["action"] = "BUY"
            row = next(row for row in output["decisions"] if row["symbol"] == "AAA")
            row["action"] = "buy"
            row["source_ids"].append(observed.get("source_id", "ds_" + "b" * 24))
            if model_buy == "own_and_topic":
                row["source_ids"].append(observed["topic_id"])
        return output

    execution, root, args, _, denied, attempts = required_fixture(tmp_path, monkeypatch,
                                                                  corrupt_answer=model_answer)
    if aligned_close:
        execution = _with_clock(execution, "2026-10-20T14:45:00+00:00")
    else:
        execution.admitted_at = datetime.fromisoformat("2026-10-20T14:45:00+00:00")
    admission = _admission(execution)
    admission["clock"]["admitted_at_utc"] = execution.admitted_at.isoformat()
    original_risk = pipeline.RiskManagerAgent.execute

    def approve_one(agent, context):
        original_risk(agent, context)
        assert not context["picks"]
        pick = TradePick("AAA", 8.2, 6.0, "C", 95.0, 91.0, 70, .0665, 280.0, time_stop_days=5)
        context.update(picks=[pick], action="HOLD", pick_rationale={"AAA": {
            "entry_setup": {"template": "C", "no_chase": False, "entry_ceiling": None},
            "signal_adv_usd": 100000000, "size_ratio": .35,
        }})
        return [pick]

    monkeypatch.setattr(pipeline.RiskManagerAgent, "execute", approve_one)
    original_post = DeepSeekRequiredClient._post_json

    def provider(client, path, request):
        stage = client.execution._active.spec.name
        if stage != "decision.debate" or path != "/v1/tools/search":
            payload, fingerprint = original_post(client, path, request)
            if stage == "search.collect" and "topic_id" not in observed:
                item = payload["results"][0]
                identity = {"url": item["url"], "title": item["title"],
                            "content": item["content"], "published": item["date"]}
                observed["topic_id"] = "ds_" + _hash(identity)[:24]
            return payload, fingerprint
        candidate_requests.append(request)
        if source == "unavailable":
            raise RequiredModelUnavailable("NETWORK_TIMEOUT")
        item = _filing()
        if source == "foreign":
            item = _filing("BBB")
        if source == "stale":
            item = _filing(published="2026-06-20T19:00:00Z")
        if source == "future":
            item = _filing(published="2026-10-20T19:00:00Z")
        if source == "date-only":
            item = _filing(published="2026-10-20T00:00:00Z")
        if source == "unofficial":
            item = _filing(url=URL.replace("www.sec.gov", "sec.gov.example.test"))
        if source == "summary-only":
            item["summary"] = item.pop("content")
        if source == "valid":
            identity = {"url": item["url"], "title": item["title"],
                        "content": item["content"], "published": item["date"]}
            observed["source_id"] = "ds_" + _hash(identity)[:24]
        payload = {"results": [] if source == "empty" else [item]}
        return payload, _hash(payload)

    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", provider)
    return execution, root, args, admission, denied, attempts, candidate_requests, observed


@pytest.mark.parametrize("source,expected_status", [
    ("valid", "filing_verified"), ("empty", "no_results"),
    ("unavailable", "search_unavailable"), ("foreign", "no_eligible_filing"),
    ("stale", "no_eligible_filing"), ("future", "no_eligible_filing"),
    ("date-only", "no_eligible_filing"), ("unofficial", "no_eligible_filing"),
    ("summary-only", "no_eligible_filing"),
])
def test_company_search_coverage_stays_separate_from_model_buy(tmp_path, monkeypatch, source, expected_status):
    execution, root, args, admission, denied, attempts, candidate_requests, observed = _fixture(
        tmp_path, monkeypatch, source=source)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    result = asdict(output.result)
    assert not denied and result["action"] == "WAIT" and result["picks"] == []
    assert candidate_requests == [{"query": candidate_searches(["AAA"], execution.run_date)["AAA"],
                                    "count": 4, "need_content": True}]
    assert attempts.count(("search.collect", "/v1/tools/search")) == 14
    assert result["raw"]["model"]["search_count"] == 14
    assert result["raw"]["debate"]["company_evidence"] == {
        "contract": COMPANY_EVIDENCE_CONTRACT,
        "candidates": {"AAA": {"status": expected_status,
                               "source_ids": [observed["source_id"]] if source == "valid" else []}}}
    assert verify_events(root, execution.run_id, daily_stage_contract(True))["stage_count"] == 39
    assert verify_required_model(root, result, admission, company_evidence_contract=COMPANY_EVIDENCE_CONTRACT) == result["raw"]["model"]


@pytest.mark.parametrize("source", ["foreign", "stale", "future", "empty"])
def test_model_cannot_force_buy_without_its_own_filing(tmp_path, monkeypatch, source):
    execution, _, args, _, _, _, _, _ = _fixture(tmp_path, monkeypatch, source=source, model_buy=True)
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)


def test_eligible_own_filing_does_not_allow_citing_a_background_article(tmp_path, monkeypatch):
    execution, _, args, _, _, _, _, _ = _fixture(tmp_path, monkeypatch, model_buy="own_and_topic")
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)


@pytest.mark.parametrize("aligned_close", [False, True])
def test_valid_own_filing_can_preserve_existing_paper_buy_and_independent_proof(tmp_path, monkeypatch, aligned_close):
    execution, root, args, admission, denied, _, _, observed = _fixture(
        tmp_path, monkeypatch, model_buy=True, aligned_close=aligned_close)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    result = asdict(output.result)
    assert not denied and result["action"] == "BUY"
    assert [row["ticker"] for row in result["picks"]] == ["AAA"]
    assert result["raw"]["execution_decisions"][0]["sources"] == [{
        "id": observed["source_id"], "title": "Fixture issuer filing", "url": URL,
        "published_at": "2026-10-20T12:00:00Z"}]
    assert [(row["ticker"], row["shares"]) for row in output.sim_state["pending"]] == [("AAA", 70)]
    if aligned_close:
        assert result["raw"]["freshness"]["benchmark_last_bar"] == "2026-10-19"
        assert result["raw"]["execution_decisions"][0]["market_evidence"][0]["as_of"] == "2026-10-19"
        assert result["raw"]["scan_stats"]["entry_setup_supplement"]["unavailable"] == 0
        verify_close_evidence(root, admission, result["raw"]["freshness"])
    assert verify_required_model(root, result, admission, company_evidence_contract=COMPANY_EVIDENCE_CONTRACT) == result["raw"]["model"]


def test_independent_proof_rejects_rehashed_company_query_for_another_symbol(tmp_path, monkeypatch):
    execution, root, args, admission, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    result = asdict(main._daily(args, "yahoo", execution=execution).result)
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == "decision.debate" and row["kind"] == "end")
    stage = read_json(root, end["output_ref"]["path"])
    call = stage["value"]["io_calls"][0]
    request = read_json(root, call["input_ref"]["path"])
    proof = read_json(root, call["output_ref"]["path"])
    query = request["value"]["request"]["query"].replace('"AAA"', '"BBB"')
    request["value"]["request"]["query"] = query
    for target in (request, proof):
        target["value"]["query_sha256"] = hashlib.sha256(query.encode()).hexdigest()
        target["value"]["request_sha256"] = _hash(request["value"]["request"])
    call["input_ref"], call["output_ref"] = _save_snapshot(root, request), _save_snapshot(root, proof)
    _rewrite_events(root, events, [(end["output_ref"], _save_snapshot(root, stage))])
    assert verify_events(root, execution.run_id, daily_stage_contract(True))["stage_count"] == 39
    with pytest.raises(EvidenceInvalid, match="Required model/search evidence"):
        verify_required_model(root, result, admission, company_evidence_contract=COMPANY_EVIDENCE_CONTRACT)


def test_company_search_targets_are_bounded_and_safe():
    day = date(2026, 10, 20)
    longest = "A1234567890.B-C"
    assert len(candidate_searches([longest], day)[longest]) <= 128
    for symbols in (["A"] * 2, [f"A{i}" for i in range(8)], ['A" site:attacker.test']):
        with pytest.raises(RequiredModelFailed):
            candidate_searches(symbols, day)
