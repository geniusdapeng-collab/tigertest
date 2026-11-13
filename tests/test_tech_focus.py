from __future__ import annotations

from datetime import datetime, timezone
import time

import pandas as pd

from trading_system import config
from trading_system.agent_runtime import (
    TECH_INTELLIGENCE_PROFILE,
    policy_manifest,
)
from trading_system.chains import CHAINS
from trading_system.data_models import StockCandidate
from trading_system.intelligence_workflow import build_frontier, verify_frontier
from trading_system.llm.intelligence_required import acquisition_topics
from trading_system.pipeline import _batch_with_fallback
from trading_system.tech_focus import (
    FOCUS_POLICY,
    TECH_FOCUS_UNIVERSE,
    build_decision_surface,
    research_cells,
    research_topics,
    scan_filters,
    select_frontier_rows,
    themes_for_symbol,
)
from trading_system.universe import RETIRED_UNIVERSE, load_universe


def _frame(price: float, periods: int = 320) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-01", periods=periods)
    close = pd.Series([price + i * 0.04 for i in range(periods)], index=index)
    return pd.DataFrame({
        "Open": close,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": 5_000_000.0,
    })


def test_focus_universe_is_the_two_reviewed_parent_chains_only():
    reviewed = {
        ticker for chain_id in FOCUS_POLICY.parent_chains
        for link in ("upstream", "midstream", "downstream")
        for ticker in CHAINS[chain_id][link]["tickers"]
        if "." not in ticker and ticker not in RETIRED_UNIVERSE
    }
    assert set(TECH_FOCUS_UNIVERSE) == reviewed
    assert len(TECH_FOCUS_UNIVERSE) == 79
    assert load_universe("tech") == list(TECH_FOCUS_UNIVERSE)
    assert all(themes_for_symbol(symbol) for symbol in TECH_FOCUS_UNIVERSE)


def test_focus_scan_filters_are_stricter_without_changing_white_paper_defaults():
    filt = scan_filters()
    assert filt["min_price"] == 10.0 > config.SCAN_MIN_PRICE
    assert filt["min_adv"] == 100_000_000 > config.SCAN_MIN_ADV_USD
    assert filt["min_history"] == 260
    assert config.SCAN_MIN_PRICE == 5.0
    assert config.SCAN_MIN_ADV_USD == 20_000_000


def test_frontier_selection_reserves_slots_for_both_parent_themes():
    ranked = [
        {"ticker": "MU", "rank": 10.0},
        {"ticker": "SNPS", "rank": 9.8},
        {"ticker": "NVDA", "rank": 9.6},
        {"ticker": "PLTR", "rank": 9.4},
        {"ticker": "MSFT", "rank": 9.2},
        {"ticker": "NET", "rank": 9.0},
        {"ticker": "AMAT", "rank": 8.8},
        {"ticker": "CRM", "rank": 8.6},
    ]
    selected = select_frontier_rows(ranked, limit=6, theme_minimum=2)
    assert len(selected) == 6
    themes = {theme for row in selected for theme in themes_for_symbol(row["ticker"])}
    assert themes == {"semiconductor_cycle", "ai_infrastructure"}


def test_research_graph_covers_six_tech_subchains_macro_sectors_and_security_evidence():
    frontier = {
        "symbols": ["MU", "NVDA"],
        "security_ids": {"MU": "us:MU", "NVDA": "us:NVDA"},
    }
    cells = research_cells(frontier)
    targets = {cell["target_id"] for cell in cells}
    assert {f"chain:{name}" for name in config.TECH_FOCUS_SUBCHAINS} <= targets
    assert {"sector:XLK", "sector:SMH", "global:macro"} <= targets
    for security in ("us:MU", "us:NVDA"):
        materials = {
            cell["material_type"] for cell in cells
            if cell["target_id"] == "security:" + security
        }
        assert materials == {"issuer_financial", "adverse_event", "counterevidence"}
    assert len(cells) == 21
    assert len({cell["cell_id"] for cell in cells}) == len(cells)


def test_focus_topics_record_only_the_sector_mappings_present_in_its_reviewed_graph():
    mapped = acquisition_topics(research_topics())
    assert {"XLK", "SMH"} <= mapped.keys()
    assert {"XLE", "XLF", "XLV", "IBB", "XLY", "XLP", "XLI", "XLU", "XLRE", "IWM"}.isdisjoint(mapped)


def test_focus_frontier_is_bounded_reproducible_and_keeps_both_themes():
    symbols = ["MU", "NVDA", "MSFT", "PLTR"]
    market = {
        "spy": _frame(500),
        "stock_ohlcv": {symbol: _frame(100 + index * 20)
                        for index, symbol in enumerate(symbols)},
    }
    as_of = datetime(2026, 11, 12, 21, tzinfo=timezone.utc)
    options = {
        "as_of": as_of,
        "market_snapshot_ref": {"sha256": "a" * 64},
        "scan_filters": scan_filters(),
        "sector_symbols": list(FOCUS_POLICY.sector_etfs),
        "top_n": FOCUS_POLICY.frontier_size,
        "focus_profile": FOCUS_POLICY.to_dict(),
    }
    frontier = build_frontier(
        market,
        symbols,
        {symbol: "us:" + symbol for symbol in symbols},
        **options,
    )
    assert len(frontier["symbols"]) == 4
    assert frontier["focus"]["theme_selected_counts"] == {
        "ai_infrastructure": 2,
        "semiconductor_cycle": 2,
    }
    assert frontier["focus"]["policy_sha256"] == FOCUS_POLICY.sha256
    verify_frontier(
        frontier,
        market,
        symbols,
        **options,
    )


def test_decision_surface_never_collapses_to_a_bare_no_pick_message():
    blocked = StockCandidate(
        ticker="MU",
        sector_etf="SMH",
        chain_id="semis",
        price=200.0,
        key_level=198.0,
        stop_price=190.0,
        tss_final=8.9,
        entry_template="",
    )
    conditional = StockCandidate(
        ticker="NVDA",
        sector_etf="XLK",
        chain_id="ai_compute",
        price=180.0,
        key_level=178.0,
        stop_price=170.0,
        tss_final=7.5,
        entry_template="B",
    )
    context = {
        "action": "WAIT",
        "market_view": "闸门尚未同时通过",
        "watchlist": [blocked, conditional],
        "picks": [],
        "pick_rationale": {},
        "tech_signals": [],
        "tech_signal_map": {},
        "chain_map": {},
        "rejections": {
            "candidate_count": 2,
            "status_counts": {"rejected": 2},
            "overlapping_reason_counts": {"tss_below_standard": 1},
            "candidates": [
                {"ticker": "MU", "status": "rejected", "reasons": ["tss_below_standard"]},
                {"ticker": "NVDA", "status": "rejected", "reasons": ["tss_below_standard"]},
            ],
        },
        "focus_research_status": {
            "state": "partial",
            "gaps": [{
                "target_id": "chain:memory",
                "target_label": "存储产业链",
                "question": "库存周期是否反转",
                "status": "insufficient_evidence",
                "reason": "no_qualified_material_for_this_question",
            }],
        },
    }
    board = build_decision_surface(context, final=True)
    watch = {row["ticker"]: row for row in board["conditional_watchlist"]}
    assert board["actionable"] == []
    assert watch["MU"]["state"] == "SETUP_PENDING"
    assert "entry_template_missing" in watch["MU"]["reasons"]
    assert watch["NVDA"]["state"] == "CONDITIONAL"
    assert watch["NVDA"]["reopen_if"]
    assert board["research_gaps"][0]["target"] == "存储产业链"
    assert board["next_open_conditions"]


def test_tech_intelligence_profile_activates_focus_contract_without_relaxing_thresholds():
    policy = policy_manifest(TECH_INTELLIGENCE_PROFILE)
    assert policy["profile"] == TECH_INTELLIGENCE_PROFILE
    assert policy["universe"] == "tech"
    assert policy["strategy"] == "tech-intelligence-first-v1"
    assert policy["tech_focus"] == FOCUS_POLICY.to_dict()
    assert policy["intelligence"]["frontier"]["ranked"] == FOCUS_POLICY.frontier_size
    assert policy["model_contract"]["required"] is True


def test_focus_market_batch_deadline_returns_partial_without_entering_fallback(monkeypatch):
    class SlowProvider:
        name = "slow-fixture"

        def ohlcv_batch(self, tickers, days=400):
            time.sleep(0.03)
            return {}

    from trading_system import pipeline
    monkeypatch.setattr(pipeline, "_ordered_batch_chain", lambda execution=None: [])
    pipeline._DATA_ISSUES.clear()
    started = time.monotonic()
    data = _batch_with_fallback(
        SlowProvider(),
        ["MU", "NVDA"],
        days=400,
        label="deadline-fixture",
        deadline_seconds=0.01,
        chunk_size=1,
    )
    assert data == {}
    assert time.monotonic() - started < 0.5
    assert any(row["issue"] == "batch_deadline_exhausted"
               for row in pipeline._DATA_ISSUES)
