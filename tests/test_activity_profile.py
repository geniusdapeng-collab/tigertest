"""Activity repairs must recover evidence without weakening any trading gates."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.agents.sector_agent import SectorAgent
from trading_system.data_models import MRSResult, PipelineResult, SectorScore, StockCandidate


ACTIVE = "us-public-active-paper-v1"


def market(mrs=6.5):
    return MRSResult(mrs_raw=mrs, delta=0, k=1, mrs_star=mrs,
                     position_cap=(0.4, 0.7), allow_new_positions=True)


def candidate(ticker="CDW", tss=8.0):
    return StockCandidate(ticker=ticker, sector_etf="XLY", price=100,
                          stop_price=95, tss=tss, tss_final=tss, c_liq=1,
                          atr_pct=0.03, entry_template="C")


def test_active_overflow_main_sector_retains_qualified_sub_pool(monkeypatch):
    sectors = [SectorScore(etf=etf, shs=score, breadth=80)
               for etf, score in [("XLK", 8.5), ("XLE", 8.2), ("XLY", 8.0)]]
    agent = SectorAgent(None)
    monkeypatch.setattr(agent, "_score_sector",
                        lambda etf, *_: deepcopy(next(s for s in sectors if s.etf == etf)))
    context = {"activity_profile": ACTIVE, "market_data": {
        "tnx": None, "spy": pd.DataFrame({"Close": [100]}),
        "universe_closes": {}, "sector_etfs": {
            s.etf: pd.DataFrame({"Close": [100] * 120}) for s in sectors}}}
    result = agent.execute(context)
    assert sum(s.in_main_pool for s in result) == 2
    assert result[2].in_sub_pool, "Third qualified sector must retain the existing light channel"


def test_active_known_sector_evidence_survives_missing_chain():
    context = {"activity_profile": ACTIVE, "mrs": market(),
               "watchlist": [candidate()],
               "sectors": [SectorScore(etf="XLY", shs=7.3, breadth=70,
                                        in_sub_pool=True)],
               "chain_map": {}, "market_data": {"stock_ohlcv": {}}}
    picks = RiskManagerAgent(None).execute(context)
    assert [p.ticker for p in picks] == ["CDW"], "Observed sector must not be replaced by neutral SHS 5"
    assert picks[0].risk_usd <= 100000 * 0.008
    assert picks[0].position_pct <= 0.20


def test_legacy_profile_decisions_are_preserved_for_unmapped_candidate():
    context = {"mrs": market(), "watchlist": [candidate()],
               "sectors": [SectorScore(etf="XLY", shs=7.3, breadth=70,
                                        in_sub_pool=True)],
               "chain_map": {}, "market_data": {"stock_ohlcv": {}}}
    assert RiskManagerAgent(None).execute(context) == []


def active_context(mrs=6.5, ticker="CDW", tss=8.0):
    return {"activity_profile": ACTIVE, "mrs": market(mrs),
            "trade_date": "2026-10-22", "watchlist": [candidate(ticker, tss)],
            "sectors": [SectorScore(etf="XLY", shs=7.3, breadth=70, in_sub_pool=True)],
            "chain_map": {}, "market_data": {"stock_ohlcv": {}}}


def signal(day="2026-10-22"):
    context = active_context()
    context["trade_date"] = day
    picks = RiskManagerAgent(None).execute(context)
    return PipelineResult(trade_date=day, provider="yahoo", mrs=context["mrs"], picks=picks,
                          action="BUY", raw={"activity_profile": ACTIVE,
                          "entry_budget": context["entry_budget"],
                          "pick_rationale": context["pick_rationale"], "account_usd": 100000})


@pytest.mark.parametrize("mrs,tss,allowed", [
    (5.17, 7.8, True), (5.17, 7.79, False), (6.5, 7.79, False),
])
def test_activity_repair_uses_existing_light_threshold(mrs, tss, allowed):
    context = active_context(mrs=mrs, tss=tss)
    assert bool(RiskManagerAgent(None).execute(context)) is allowed
    assert context["rejections"]["candidate_count"] == 1
    assert sum(context["rejections"]["status_counts"].values()) == 1
    if allowed:
        assert all(gate["ok"] for gate in context["pick_rationale"]["CDW"]["gate"].values())


def test_unfunded_candidates_do_not_consume_active_position_slots():
    from trading_system.chains import CHAINS
    context = active_context()
    context["sectors"] = [SectorScore(etf="SMH", shs=8, breadth=80, in_main_pool=True),
                          SectorScore(etf="XLK", shs=8, breadth=80, in_main_pool=True)]
    semis = [candidate(f"SEMI{i}") for i in range(7)]
    for c in semis:
        c.chain_id = "semis"
    team = candidate("TEAM")
    team.chain_id = "ai_compute"
    context["watchlist"] = semis + [team]
    context["portfolio_state"] = {"positions": [{"ticker": "ASML", "shares": 100,
        "entry_price": 100, "last_price": 100, "stop": 70, "chain": "semis", "sector": "SMH"}]}
    assert CHAINS["ai_compute"]["etf"] == "XLK"
    assert [p.ticker for p in RiskManagerAgent(None).execute(context)] == ["TEAM"]


@pytest.mark.parametrize("bar_kind", ["missing", "stale"])
def test_expired_activity_pending_releases_reserved_budget_without_fresh_bar(tmp_path, bar_kind):
    from trading_system.simulator import Bar, SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    yesterday = signal("2026-10-20")
    engine.step(yesterday.trade_date, yesterday, lambda _: None)
    today = PipelineResult(trade_date="2026-10-22", provider="yahoo", mrs=market(),
                           action="WAIT", raw={"activity_profile": ACTIVE})
    out = engine.step(today.trade_date, today, lambda _: None if bar_kind == "missing" else
                      Bar(100, 101, 99, 100, date="2026-10-20"))
    assert out["fill_checks"][0]["reason"] == "execution_window_missed"
    assert not engine.state["positions"] and not engine.state["pending"]


def test_unknown_sector_is_not_invented_even_in_mrs_probe_band():
    context = active_context(mrs=5.8, ticker="NOT-A-KNOWN-SYMBOL")
    assert not RiskManagerAgent(None).execute(context)
    assert context["rejections"]["candidates"][0]["reasons"] == ["sector_evidence_missing"]


def test_existing_and_pending_exposure_reserves_account_budget():
    context = active_context()
    context["portfolio_state"] = {"positions": [{"ticker": "OTHER", "shares": 700,
        "entry_price": 100, "last_price": 100, "stop": 99, "chain": "", "sector": "XLY"}]}
    assert not RiskManagerAgent(None).execute(context)
    assert "gross_budget_exhausted" in context["rejections"]["overlapping_reason_counts"]


@pytest.mark.parametrize("profile", ["legacy", "active"])
@pytest.mark.parametrize("scenario,reason,expected_shares", [
    ("pending_duplicate", "existing_or_pending_position", 0),
    ("whole_gross", "gross_budget_exhausted", 0),
    ("whole_group", "group_risk_budget_exhausted", 0),
    ("remaining_group", None, 20),
])
def test_pending_and_held_reservations_cover_duplicates_gross_and_group_boundary(
        profile, scenario, reason, expected_shares):
    context = active_context()
    if profile == "legacy":
        context.pop("activity_profile")
        context["paper_execution_budgeted"] = True
        # Legacy uses the original chain ETF rather than the optional proxy.
        context["watchlist"][0].chain_id = "ai_compute"
        context["sectors"][0].etf = "XLK"
    sector = context["sectors"][0].etf
    chain = "ai_compute" if profile == "legacy" else ""
    held = {"ticker": "OTHER", "shares": 100, "entry_price": 100,
            "last_price": 100, "stop": 95, "chain": chain, "sector": sector}
    pending = {"ticker": "PLANNED", "shares": 100, "entry_ref": 100,
               "stop_price": 75, "risk_usd": 2500, "chain": chain, "sector": sector}
    if scenario == "pending_duplicate":
        pending.update(ticker="CDW", shares=10, stop_price=95, risk_usd=50)
    elif scenario == "whole_gross":
        held.update(shares=400, stop=99)
        pending.update(shares=300, stop_price=95, risk_usd=1500)
    elif scenario == "remaining_group":
        pending.update(stop_price=76, risk_usd=2400)
    context["portfolio_state"] = {"positions": [held], "pending": [pending]}
    picks = RiskManagerAgent(None).execute(context)
    assert (picks[0].shares if picks else 0) == expected_shares
    if reason:
        assert context["rejections"]["candidates"][0]["reasons"] == [reason]
    else:
        assert picks[0].risk_usd + 500 + 2400 == 3000  # Exactly the existing 3% group bound.


def test_current_close_cannot_retroactively_block_signal_next_open(tmp_path):
    from trading_system.simulator import Bar, SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    yesterday = signal()
    engine.step(yesterday.trade_date, yesterday, lambda _: None)
    assert engine.state["pending"] and not engine.state["positions"]
    today_mrs = market(3.0)
    today_mrs.allow_new_positions = False
    today = PipelineResult(trade_date="2026-10-23", provider="yahoo", mrs=today_mrs,
                           action="AVOID", raw={"activity_profile": ACTIVE})
    out = engine.step(today.trade_date, today,
                      lambda _: Bar(100, 101, 99, 100, date="2026-10-23"))
    assert len(engine.state["positions"]) == 1
    assert out["fill_checks"][0]["reason"] == "accepted"
    assert out["fill_checks"][0]["risk_after"] <= 0.008 * 0.35
    assert not engine.state["pending"]


def test_activity_missed_execution_window_does_not_invent_later_fill(tmp_path):
    from trading_system.simulator import Bar, SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    yesterday = signal("2026-10-20")
    engine.step(yesterday.trade_date, yesterday, lambda _: None)
    today = PipelineResult(trade_date="2026-10-22", provider="yahoo", mrs=market(),
                           action="WAIT", raw={"activity_profile": ACTIVE})
    out = engine.step(today.trade_date, today,
                      lambda _: Bar(100, 101, 99, 100, date="2026-10-22"))
    assert out["fill_checks"][0]["reason"] == "execution_window_missed"
    assert not engine.state["positions"] and not engine.state["pending"]


def test_opening_cost_uses_frozen_signal_adv_not_same_day_turnover(tmp_path):
    from trading_system.simulator import Bar, SimEngine
    outcomes = []
    for name, adv in [("thin", 1_000_000), ("thick", 10_000_000_000)]:
        engine = SimEngine(str(tmp_path / (name + ".json")))
        yesterday = signal()
        engine.step(yesterday.trade_date, yesterday, lambda _: None)
        today = PipelineResult(trade_date="2026-10-23", provider="yahoo", mrs=market(),
                               action="WAIT", raw={"activity_profile": ACTIVE})
        out = engine.step(today.trade_date, today,
                          lambda _: Bar(100, 101, 99, 100, adv=adv, date="2026-10-23"))
        pos = engine.state["positions"][0]
        outcomes.append((pos["shares"], pos["entry_price"], out["fill_checks"][0]))
    assert outcomes[0] == outcomes[1]


def test_opening_budget_values_holdings_at_open_and_shrinks_signal_quantity(tmp_path):
    from trading_system.paper_policy import opening_allocation
    from trading_system.simulator import Bar, SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    yesterday = signal()
    engine.step(yesterday.trade_date, yesterday, lambda _: None)
    pending = deepcopy(engine.state["pending"][0])
    pending["shares"] = 10_000
    pending["stop_price"] = 98
    pending["entry_budget"]["gross_cap"] = 0.25
    state = {"cash": 90_000, "positions": [{"ticker": "OTHER", "shares": 100,
             "entry_price": 100, "last_price": 1, "stop": 95, "sector": "XLY"}]}
    observed = lambda _: (Bar(200, 201, 199, 200, date="2026-10-23"), "2026-10-23")
    allocation = opening_allocation(state, pending, None, "2026-10-23", observed, 100.1)
    assert allocation["opening_equity"] == 110_000
    assert allocation["shares"] == 74  # gross: (110000*25%-20000)/100.1
    assert allocation["gross_after"] <= 0.25
    assert allocation["risk_after"] <= 0.008 * 0.35


@pytest.mark.parametrize("field,value", [("gross_cap", float("nan")),
    ("risk_pct", 0.009), ("max_positions", 8), ("entry_friction_bps", 0),
    ("execution_date", "2026-10-26")])
def test_opening_budget_rejects_corrupt_or_overlarge_limits(tmp_path, field, value):
    from trading_system.paper_policy import opening_allocation
    from trading_system.simulator import SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    yesterday = signal()
    engine.step(yesterday.trade_date, yesterday, lambda _: None)
    pending = deepcopy(engine.state["pending"][0])
    pending["entry_budget"][field] = value
    allocation = opening_allocation({"cash": 100000, "positions": []}, pending,
                                   None, "2026-10-23", lambda _: None, 100.1)
    assert not allocation["shares"]
    assert allocation["reason"] in {"signal_budget_invalid", "signal_budget_date_invalid"}


def test_both_profiles_remain_explicit_and_v1_policy_bytes_unchanged():
    from trading_system.agent_evidence import json_bytes
    from trading_system.agent_runtime import policy_manifest
    from trading_system.agent_runner import validate_request
    # Original profile remains a distinct, unchanged policy contract.
    legacy = policy_manifest()
    assert json_bytes(legacy) == (Path(__file__).parent / "fixtures/public-paper-policy-v1.json").read_bytes()
    assert legacy["profile"] == "us-public-paper-v1"
    assert legacy["strategy"] == "reviewed_source_defaults"
    assert legacy["execution"]["policy_id"] == "tiger.public-paper-policy.v1"
    active = policy_manifest(ACTIVE)
    assert active["execution"]["policy_id"] == "tiger.public-active-paper-policy.v1"
    assert legacy["stages"] == active["stages"]
    for field in ["initial_cash_usd", "use_tuned", "real_broker", "universe"]:
        assert legacy[field] == active[field]
    request = {"schema_version": "tiger.agent-request/v1", "profile": ACTIVE,
               "paper_account": "public-usd", "idempotency_key": "activity-request-001"}
    assert validate_request("run", request) == request
    for invalid in [None, [], {}, "unreviewed-profile"]:
        with pytest.raises(ValueError):
            validate_request("run", request | {"profile": invalid})


def test_legacy_strict_uses_same_budget_guards_without_enabling_sector_experiment():
    context = active_context()
    context.pop("activity_profile")
    context["paper_execution_budgeted"] = True
    assert not RiskManagerAgent(None).execute(context)  # CDW legacy stays unmapped.
    context["watchlist"][0].chain_id = "ai_compute"
    context["sectors"] = [SectorScore(etf="XLK", shs=8, breadth=80, in_main_pool=True)]
    assert RiskManagerAgent(None).execute(context)
    context["portfolio_state"] = {"positions": [{"ticker": "OTHER", "shares": 700,
        "entry_price": 100, "last_price": 100, "stop": 99, "chain": "", "sector": "XLY"}]}
    assert not RiskManagerAgent(None).execute(context)


def test_legacy_strict_never_invents_budget_for_old_pending_signal(tmp_path):
    from trading_system.simulator import Bar, SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.state["pending"] = [{"ticker": "CDW", "shares": 50, "stop_price": 95,
        "risk_usd": 250, "signal_date": "2026-10-22"}]
    result = PipelineResult(trade_date="2026-10-23", provider="yahoo", mrs=market(),
        action="WAIT", raw={"paper_execution_budgeted": True,
                            "paper_execution_version": "signal-budgeted-open-v1"})
    out = engine.step(result.trade_date, result,
        lambda _: Bar(100, 101, 99, 100, date="2026-10-23"))
    assert out["fill_checks"][0]["reason"] == "signal_budget_missing"
    assert not engine.state["pending"] and not engine.state["positions"]


@pytest.mark.parametrize("profile", ["legacy", "active"])
@pytest.mark.parametrize("template", ["", "D", None])
def test_strict_profiles_never_trade_without_a_recognized_entry_setup(profile, template):
    context = active_context(mrs=6.5, tss=10)
    if profile == "legacy":
        context.pop("activity_profile")
        context["paper_execution_budgeted"] = True
    context["watchlist"][0].chain_id = "ai_compute"
    context["watchlist"][0].entry_template = template
    context["sectors"] = [SectorScore(etf="XLK", shs=9, breadth=80, in_main_pool=True)]
    assert not RiskManagerAgent(None).execute(context)
    assert context["rejections"]["candidates"][0]["reasons"] == ["entry_template_missing"]


@pytest.mark.parametrize("profile", ["legacy", "active"])
@pytest.mark.parametrize("breadth,key,allowed", [(39.9, 99, False), (40, 99, True), (39.9, 100, True)])
def test_low_breadth_forbids_chasing_but_keeps_pullback_and_exact_40_boundary(profile, breadth, key, allowed):
    context = active_context()
    if profile == "legacy":
        context.pop("activity_profile")
        context["paper_execution_budgeted"] = True
    context["watchlist"][0].chain_id = "ai_compute"
    context["watchlist"][0].key_level = key
    context["sectors"] = [SectorScore(etf="XLK", shs=7.3, breadth=breadth, in_sub_pool=True)]
    assert bool(RiskManagerAgent(None).execute(context)) is allowed
    if not allowed:
        assert context["rejections"]["candidates"][0]["reasons"] == ["sector_breadth_no_chase"]


def test_low_breadth_signal_cannot_gap_into_breakout_at_next_open(tmp_path):
    from trading_system.simulator import Bar, SimEngine
    context = active_context()
    context["sectors"][0].breadth = 39.9
    context["watchlist"][0].key_level = 100
    picks = RiskManagerAgent(None).execute(context)
    result = PipelineResult(trade_date="2026-10-22", provider="yahoo", mrs=context["mrs"],
        picks=picks, action="HOLD", raw={"activity_profile": ACTIVE,
            "entry_budget": context["entry_budget"], "pick_rationale": context["pick_rationale"]})
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.step(result.trade_date, result, lambda _: None)
    today = PipelineResult(trade_date="2026-10-23", provider="yahoo", mrs=market(),
        action="WAIT", raw={"paper_execution_budgeted": True})
    out = engine.step(today.trade_date, today,
        lambda _: Bar(101, 102, 99, 100, date="2026-10-23"))
    assert out["fill_checks"][0]["reason"] == "sector_breadth_no_chase"
    assert not engine.state["positions"] and not engine.state["pending"]


def test_refused_opening_cannot_be_retried_after_same_day_exits_free_slots(tmp_path):
    from trading_system.simulator import Bar, SimEngine
    engine = SimEngine(str(tmp_path / "paper.json"))
    yesterday = signal()
    engine.step(yesterday.trade_date, yesterday, lambda _: None)
    engine.state["positions"] = [{"ticker": f"HELD{i}", "shares": 1, "entry_price": 100,
        "stop": 95, "entry_date": "2026-10-21", "last_bar_date": "2026-10-22",
        "bars_held": 1, "risk_usd": 5, "last_price": 100, "peak_price": 100}
        for i in range(7)]
    today = PipelineResult(trade_date="2026-10-23", provider="yahoo", mrs=market(),
        action="WAIT", raw={"paper_execution_budgeted": True})
    def observed(ticker):
        return (Bar(94, 95, 93, 94, date="2026-10-23") if ticker.startswith("HELD") else
                Bar(100, 101, 99, 100, date="2026-10-23"))
    out = engine.step(today.trade_date, today, observed)
    assert out["fill_checks"][0]["reason"] == "position_slots_exhausted"
    assert not engine.state["positions"]  # Existing holdings exited later in the session.
    assert not engine.state["pending"]  # Cannot reuse their later freed slots at an earlier open.
    engine.step(today.trade_date, today, observed)
    assert not engine.state["positions"]
