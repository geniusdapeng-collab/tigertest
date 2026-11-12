from __future__ import annotations

import pandas as pd

from trading_system.agents.tss_agent import TSSAgent
from trading_system.agents.universe_scanner_agent import UniverseScannerAgent


class OptionsRecorder:
    def __init__(self):
        self.tickers = []

    def options_chain_snapshot(self, ticker):
        self.tickers.append(ticker)
        return None


def _market():
    index = pd.bdate_range("2025-01-01", periods=220)
    close = pd.Series([100 + i / 10 for i in range(len(index))], index=index)
    base = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 2_000_000.0})
    pullback = base.copy()
    pullback.loc[index[-5:], "Volume"] = 1_000_000.0
    thin = pullback.copy()
    thin["Volume"] /= 100
    return {"spy": base, "stock_ohlcv": {"AAA": base, "BBB": pullback, "CCC": thin}}


def test_entry_setup_below_shortlist_is_scored_without_reopening_hard_filters():
    provider = OptionsRecorder()
    market = _market()
    universe = ["AAA", "BBB", "CCC", "DDD"]

    legacy = UniverseScannerAgent(provider, universe, top_n=1).execute({"market_data": market})
    assert [c.ticker for c in legacy] == ["AAA"]

    context = {"market_data": market}
    candidates = UniverseScannerAgent(provider, universe, top_n=1,
                                      include_entry_setups=True).execute(context)
    assert [c.ticker for c in candidates] == ["AAA", "BBB"]
    assert provider.tickers == []
    assert context["scan_stats"]["passed"] == 2
    assert context["scan_stats"]["entry_setup_supplement"] == {
        "checked": 1, "added": 1, "baseline_selected": 1, "unavailable": 0,
    }

    scored = TSSAgent(provider).execute(context)
    assert {c.ticker: c.entry_template for c in scored} == {"AAA": "", "BBB": "C"}
    assert provider.tickers == ["AAA", "BBB"]


def test_existing_candidates_are_not_rescored_or_duplicated_by_the_supplement():
    provider = OptionsRecorder()
    context = {"market_data": _market()}
    candidates = UniverseScannerAgent(provider, ["BBB", "AAA"], top_n=1,
                                      include_entry_setups=True).execute(context)
    assert [c.ticker for c in candidates] == ["BBB"]
    assert context["scan_stats"]["entry_setup_supplement"]["added"] == 0
    assert provider.tickers == []


def test_empty_filtered_pool_has_an_explicit_zero_coverage_record():
    context = {"market_data": _market()}
    assert UniverseScannerAgent(OptionsRecorder(), ["CCC", "DDD"], top_n=1,
                                include_entry_setups=True).execute(context) == []
    assert context["scan_stats"]["entry_setup_supplement"] == {
        "checked": 0, "added": 0, "baseline_selected": 0, "unavailable": 0,
    }


def test_missing_structure_input_in_lower_rank_is_counted_separately():
    market = _market()
    market["stock_ohlcv"]["BBB"]["Open"] = float("nan")
    context = {"market_data": market}
    candidates = UniverseScannerAgent(OptionsRecorder(), ["AAA", "BBB"], top_n=1,
                                      include_entry_setups=True).execute(context)
    assert [c.ticker for c in candidates] == ["AAA"]
    assert context["scan_stats"]["entry_setup_supplement"] == {
        "checked": 1, "added": 0, "baseline_selected": 1, "unavailable": 1,
    }
