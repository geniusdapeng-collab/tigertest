"""Offline binding between the v4 decision prompt and independent replay."""

from dataclasses import asdict
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system.agent_evidence import EvidenceInvalid, normalize
from trading_system.data_models import TradePick
from trading_system.llm import intelligence_required, work_proof
from trading_system.llm.intelligence_proof import _verify_decision
from trading_system.redline.execution import (
    CURRENT_TECH_INTELLIGENCE_POLICY_ID,
    INTELLIGENCE_SEARCH_SOURCES,
    ExecutionPolicy,
)


@pytest.mark.parametrize("action", ["WAIT", "AVOID"])
def test_v4_reduced_evidence_replays_with_a_numerically_approved_pick(monkeypatch, action):
    stage = "decision.debate"
    symbol = "NVDA"
    security = "us:NVDA"
    execution = SimpleNamespace(
        policy=ExecutionPolicy(
            CURRENT_TECH_INTELLIGENCE_POLICY_ID, model_mode="enabled",
            search_source_names=INTELLIGENCE_SEARCH_SOURCES, reduced_evidence=True,
        ),
        run_date=date(2026, 11, 5),
        admitted_at=datetime(2026, 11, 5, 21, 30, tzinfo=timezone.utc),
    )
    prices = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-11-05"]))
    market = {"stock_ohlcv": {symbol: prices}, "spy": prices, "sector_etfs": {},
              "freshness": {"benchmark_last_bar": "2026-11-05", "benchmark_lag_days": 0}}
    frontier = {"symbols": [symbol], "security_ids": {symbol: security}}
    bundle = {"bundle_id": "bundle_fixture", "manifest_sha256": "1" * 64, "gaps": [
        {"cell_id": "missing-sec-original", "target_id": "security:" + security,
         "required": True, "status": "missing"},
    ]}
    pick = TradePick(symbol, 8, 8, "A", 101, 95, 10, 0.1, 60)
    context = {"picks": [pick], "watchlist": [], "market_data": market,
               "action": "BUY", "trade_date": execution.run_date.isoformat(),
               "mrs": {}, "sectors": [], "rejections": [], "pick_rationale": {symbol: "数值闸门通过"}}
    assessment = intelligence_required.assessment_context(execution)
    snapshots = intelligence_required.market_snapshots(market, [symbol], assessment)
    response = {"action": action, "action_summary": "等有可核验的公司原文再考虑。",
                "decisions": [{
                    "symbol": symbol, "action": "watch", "action_summary": "暂时观察。",
                    "why_this": "数值已过门槛。", "why_now": "公司原文尚未核验。",
                    "entry_condition": "先补齐公司原文证据。", "invalidation": "已有结构被破坏则失效。",
                    "risk_note": "缺少最新公司披露。", "source_ids": [snapshots[0]["id"]],
                }]}
    observed = {}

    def complete_json(**args):
        observed.update(args)
        return response

    def plan(*_args, **_kwargs):
        return {}, 100, 100

    client = SimpleNamespace(execution=execution, plan_chat=plan, complete_json=complete_json,
                             intelligence_acquisition=SimpleNamespace(frontier=frontier, bundle=bundle))
    final_context = dict(context)
    decision = intelligence_required.decision(client, final_context, [], 100_000)
    company = decision["company_evidence"]["candidates"][symbol]
    assert company["status"] == "reduced_evidence"
    assert company["missing_cells"] == ["missing-sec-original"]
    assert company["source_ids"] == []
    assert final_context["picks"] == []
    assert final_context["action"] == action

    bound = normalize({
        "picks": [pick], "watchlist": [], "mrs": context["mrs"], "sectors": context["sectors"],
        "action": context["action"], "rationale": context["pick_rationale"], "rejections": [],
        "documents": [], "assessment": assessment, "market_evidence": snapshots,
        "market_asof": intelligence_required.market_asof(market, [symbol], assessment),
        "account_usd": 100_000,
    })
    outputs = normalize({
        "layer1.mrs": context["mrs"], "layer2.sector": {"sectors": context["sectors"]},
        "layer3.tss": {"watchlist": []},
        "layer4.risk": {"picks": [pick], "action": context["action"], "rejections": [],
                        "pick_rationale": context["pick_rationale"]},
        stage: decision,
    })
    replay = SimpleNamespace(
        inputs={stage: bound, "layer4.risk": {"trade_date": context["trade_date"]}},
        value=outputs.__getitem__, assessment=assessment, cutoff=execution.admitted_at,
        policy={"execution": asdict(execution.policy), "research_work": {}},
        io=lambda _: [({"method": "tasks.child.join"}, None, None)],
        planner=lambda _: plan,
    )
    result = {"action": action, "market_view": response["action_summary"], "picks": [],
              "raw": {"debate": decision, "execution_decisions": decision["execution_decisions"],
                      "action_summary": response["action_summary"]}}

    def verify_call(_replay, _call, payload, schema, **_kwargs):
        # Join contents were captured by the real decision producer. A replay
        # with different company evidence would produce a different unit ID.
        expected = observed["public_payload"]["company_evidence"]
        assert payload["company_evidence"] == expected
        assert schema == observed["schema"]
        return response

    def verify_outcome(_replay, value, *, selected_symbols, pick_rationale):
        assert value == decision
        assert selected_symbols == []
        assert pick_rationale == {}

    monkeypatch.setattr(work_proof, "verify_decision_call", verify_call)
    monkeypatch.setattr(work_proof, "verify_decision_outcome", verify_outcome)
    _verify_decision(replay, [], frontier, bundle, normalize(market), result)

    # A v4 policy's flag must be an actual boolean; a truthy string is not a
    # valid policy value and must never authorize this replay.
    replay.policy["execution"]["reduced_evidence"] = "true"
    with pytest.raises(EvidenceInvalid, match="frozen intelligence evidence"):
        _verify_decision(replay, [], frontier, bundle, normalize(market), result)

    # Even a substituted child response cannot buy using market evidence alone.
    replay.policy["execution"]["reduced_evidence"] = True
    unauthorized_buy = {**response, "action": "BUY",
                        "decisions": [{**response["decisions"][0], "action": "buy"}]}
    monkeypatch.setattr(work_proof, "verify_decision_call", lambda *_args, **_kwargs: unauthorized_buy)
    with pytest.raises(EvidenceInvalid, match="frozen intelligence evidence"):
        _verify_decision(replay, [], frontier, bundle, normalize(market), result)
