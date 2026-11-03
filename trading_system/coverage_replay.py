"""Paired, offline comparison of candidate coverage on a sealed Tiger snapshot."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pandas as pd

from . import config
from .agent_evidence import EvidenceInvalid, checked_root, read_bytes, read_json, read_snapshot, strict_json
from .agent_runtime import verify_run
from .agents import ChainCycleAgent, MRSAgent, RiskManagerAgent, SectorAgent, TSSAgent, UniverseScannerAgent
from .currency import require_usd_sim_state
from .events import EventCalendar
from .markets import get_market
from .simulator import Bar, SimEngine

_SCHEMA = "tiger.coverage-comparison.v1"
_ACCOUNT = 100_000.0


def _periods(values):
    intervals = []
    for value in values:
        start, end = value.split(":")
        a, b = pd.Timestamp(start), pd.Timestamp(end)
        if str(a.date()) != start or str(b.date()) != end or a > b:
            raise ValueError("Use ordered YYYY-MM-DD:YYYY-MM-DD periods")
        intervals.append((a, b))
    intervals.sort()
    if not 2 <= len(intervals) <= 6:
        raise ValueError("Provide two to six disjoint periods")
    if any(a[1] >= b[0] for a, b in zip(intervals, intervals[1:])):
        raise ValueError("Periods may not overlap")
    return intervals


def _period_dates(index, period, admission):
    start, end = period
    if end >= admission:
        raise ValueError("Period must end earlier than the admission date")
    dates = index[(index >= start) & (index <= end)]
    if not 5 <= len(dates) <= 65:
        raise ValueError("Each period must include between 5 and 65 observed SPY sessions")
    return dates


def _series_or_table(item):
    index = pd.DatetimeIndex([pd.Timestamp(x["value"][:10]) for x in item["index"]])
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("Snapshot session dates must be ordered and unique")

    def number(x):
        return float("nan") if isinstance(x, dict) and x.get("__type__") == "missing" else float(x)

    kind = item["__type__"]
    if kind == "series":
        return pd.Series([number(x) for x in item["values"]], index=index)
    if kind != "dataframe":
        raise ValueError("Snapshot market values must be a series or data frame")
    return pd.DataFrame([[number(x) for x in row] for row in item["values"]],
                        columns=item["columns"], index=index)


def _load(state, run_id):
    state = checked_root(state.resolve())
    verification = verify_run(state, run_id, current_source=None)
    if verification["status"] != "completed":
        raise ValueError("Replay requires a successfully completed sealed case")
    run = state / "runs" / run_id
    admission = read_json(run, "admission.json")
    event = next(
        row for raw in read_bytes(run, "events.jsonl").splitlines()
        if (row := strict_json(raw))["stage"] == "data.prepare" and row["kind"] == "end"
    )
    data = read_snapshot(run, event["output_ref"], run_id=run_id, stage="data.prepare", role="output")["value"]
    md = data["market_data"]
    market = {name: _series_or_table(md[name]) if md[name] is not None else None
              for name in ("spy", "tnx", "vix", "vix9d")}
    for name in ("stock_ohlcv", "sector_etfs"):
        market[name] = {symbol: _series_or_table(item) for symbol, item in md[name].items()}
    return market, data["universe"], pd.Timestamp(admission["clock"]["run_date"]), verification["verification"]["receipt_sha256"]


def _asof(series, date, *, exact=False):
    if series is None:
        return None
    past = series.loc[:date]
    if past.empty or (exact and past.index[-1] != date):
        return None
    return past


def _prices(row):
    values = tuple(float(row[name]) for name in ("Open", "High", "Low", "Close"))
    if not all(math.isfinite(x) and x > 0 for x in values):
        return None
    return values


def _fresh_bars(group, date):
    return {symbol: past for symbol, series in group.items()
            if (past := _asof(series, date, exact=True)) is not None
            and _prices(past.iloc[-1]) is not None}


def _bar(series, date):
    if series is None or date not in series.index:
        return None
    values = _prices(series.loc[date])
    if values is None:
        return None
    # Intraday exits may precede the day's close and final volume.
    window = series.loc[series.index < date].tail(20)
    adv = float((window["Close"] * window["Volume"]).mean())
    return Bar(*values, adv=adv if math.isfinite(adv) and adv > 0 else 0.0,
               date=str(date.date()))


class _HistoricalProvider:
    def options_chain_snapshot(self, ticker):
        return None

    def gex_billions(self):
        return None

    def zero_dte_share(self):
        return None


class _ResearchBook(SimEngine):
    def save(self):
        # The comparison is deliberately in memory; preserve accounting validation.
        require_usd_sim_state(self.state, scope="offline coverage comparison")


def _market_for_date(market, date):
    md = {k: _asof(market[k], date, exact=k == "spy") for k in ("spy", "tnx", "vix", "vix9d")}
    for k in ("tnx", "vix", "vix9d"):
        if md[k] is not None and (date - md[k].index[-1]).days > 5:
            md[k] = None
    md["stock_ohlcv"] = _fresh_bars(market["stock_ohlcv"], date)
    md["sector_etfs"] = _fresh_bars(market["sector_etfs"], date)
    md["universe_closes"] = {t: f["Close"] for t, f in md["stock_ohlcv"].items()}
    return md


def _summary(book):
    state = book.state
    purchases = (sum(t["shares"] * t["entry"] for t in state["closed"])
                 + sum(t["shares"] * t["entry_price"] for t in state["positions"]))
    sales = sum(t["shares"] * t["exit"] for t in state["closed"])
    return {**book.stats(), "open_positions": len(state["positions"]),
            "pending_signals": len(state["pending"]),
            "turnover_initial_capital": round((purchases + sales) / book.initial_cash, 6)}


def _run_period(market, universe, dates, directory):
    provider = _HistoricalProvider()
    spec = get_market("us")
    calendar_file = directory / "calendar.json"
    calendar_file.write_text('{"macro":{},"earnings":{}}\n')
    calendar = EventCalendar(str(calendar_file), strict=True)
    books = {mode: _ResearchBook(str(directory / mode), initial_cash=_ACCOUNT)
             for mode in ("legacy_shortlist", "entry_setup_coverage")}
    risk = RiskManagerAgent(provider, account_usd=_ACCOUNT)
    rows = []
    for date in dates:
        md = _market_for_date(market, date)
        session = str(date.date())
        session_data = {"date": session, "stocks_with_exact_bar": len(md["stock_ohlcv"]),
                        "etfs_with_exact_bar": len(md["sector_etfs"])}
        ready = (md["spy"] is not None and len(md["spy"]) >= 260
                 and md["tnx"] is not None and len(md["tnx"]) >= 100
                 and md["vix"] is not None and len(md["vix"]) >= 30
                 and len(md["sector_etfs"]) >= (len(spec.sector_symbols) * 2 + 2) // 3)
        common = {"market_data": md, "trade_date": session, "market_spec": spec,
                  "market_grad": spec.graduation(), "scan_filters": spec.scan_filters(),
                  "event_calendar": calendar, "paper_execution_budgeted": True}
        if ready:
            mrs = MRSAgent(provider).execute(common)
            SectorAgent(provider).execute(common)
            ChainCycleAgent(provider).execute(common)
            UniverseScannerAgent(provider, universe, include_entry_setups=True).execute(common)
            stats = common["scan_stats"]
            supplement = stats["entry_setup_supplement"]
            # The coverage scanner retains the original ordered shortlist as its prefix.
            baseline = {c.ticker for c in common["watchlist"][:supplement["baseline_selected"]]}
            scored = TSSAgent(provider).execute(common)
            session_data.update(regime=mrs.regime, mrs=mrs.mrs_star)
        else:
            session_data["reason"] = "insufficient_aligned_benchmark_or_sector_data"
        for mode, book in books.items():
            ctx = {**common, "portfolio_state": book.state}
            if ready:
                expanded = mode == "entry_setup_coverage"
                ctx["watchlist"] = deepcopy([c for c in scored if expanded or c.ticker in baseline])
                risk.execute(ctx)
                session_data[mode] = {
                    "passed": stats["passed"], "selected": len(ctx["watchlist"]),
                    "supplement": supplement if expanded else None,
                    "approved": [p.ticker for p in ctx["picks"]], "action": ctx["action"],
                }
            else:
                ctx.update(action="AVOID", picks=[])
            result = SimpleNamespace(trade_date=session, provider="frozen-research-replay",
                                     action=ctx["action"], picks=ctx["picks"], mrs=ctx.get("mrs"),
                                     raw={k: ctx[k] for k in ("paper_execution_budgeted",
                                                              "entry_budget", "pick_rationale") if k in ctx})
            book.step(session, result, lambda ticker: _bar(md["stock_ohlcv"].get(ticker), date))
        rows.append(session_data)
    return {"from": str(dates[0].date()), "through": str(dates[-1].date()),
            "scored_sessions": sum("regime" in row for row in rows), "sessions": rows,
            "portfolios": {mode: _summary(book) for mode, book in books.items()}}


def compare(state, run_id, period_specs):
    periods = _periods(period_specs)
    market, universe, admission, receipt_hash = _load(state, run_id)
    index = market["spy"].index
    with TemporaryDirectory(prefix="tiger-coverage-") as directory:
        root = Path(directory)
        results = [_run_period(market, universe, _period_dates(index, period, admission), root)
                   for period in periods]
    return {"schema": _SCHEMA, "source_run_id": run_id, "source_receipt_sha256": receipt_hash,
            "signal_universe_size": len(universe), "periods": results,
            "limits": ["Frozen current-pool membership and vendor-adjusted prices may have survivorship and revision bias.",
                       "No historical option, semantic, candidate news or model gate inputs are fabricated.",
                       "Two paper books start flat in each interval; costs, holdings and pending signals are reported.",
                       "Exit costs use ADV through the session before execution.",
                       "This is a numerical comparison, with no inference about strategy efficacy or market-wide coverage."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--period", action="append", required=True)
    args = parser.parse_args()
    try:
        report = compare(args.state_root, args.run_id, args.period)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    except (EvidenceInvalid, OSError, ValueError) as exc:
        parser.exit(2, f"Coverage comparison unavailable: {exc}\n")


if __name__ == "__main__":
    main()
