"""Paired, offline comparison of candidate coverage on a sealed Tiger snapshot."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
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
from .market_close import _scheduled_close, macro_limits
from .security_roster import SecurityRoster
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
    observed = {}
    for symbol, series in group.items():
        past = _asof(series, date, exact=True)
        if past is None:
            continue
        last = past.iloc[-1]
        volume = float(last.get("Volume", float("nan")))
        if _prices(last) is not None and math.isfinite(volume) and volume > 0:
            observed[symbol] = past
    return observed


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


def _market_for_date(market, date, *, roster_view=None):
    macro_cutoffs = {}
    if roster_view is not None:
        close = _scheduled_close(date.date())
        if close is not None:
            macro_cutoffs = {name: pd.Timestamp(cutoff) for name, cutoff in macro_limits(close, close).items()}
    md = {k: _asof(market[k], macro_cutoffs.get(k, date), exact=k == "spy")
          for k in ("spy", "tnx", "vix", "vix9d")}
    for k in ("tnx", "vix", "vix9d"):
        if md[k] is not None and (date - md[k].index[-1]).days > 5:
            md[k] = None
    md["stock_ohlcv"] = _fresh_bars(market["stock_ohlcv"], date)
    if roster_view is not None:
        md["stock_ohlcv"] = {
            symbol: frame.loc[pd.Timestamp(roster_view.members[symbol].bars_since):]
            for symbol, frame in md["stock_ohlcv"].items() if symbol in roster_view.members}
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


def _coverage_oracle(common, universe, provider, scored, risk):
    """Check actual full TSS and risk on the same flat account and market slice."""
    full = dict(common)
    UniverseScannerAgent(provider, universe, top_n=len(universe)).execute(full)
    reference = {candidate.ticker: candidate for candidate in TSSAgent(provider).execute(full)}
    expected = {ticker for ticker, candidate in reference.items()
                if candidate.entry_template in ("A", "B", "C")}
    actual = {candidate.ticker for candidate in scored if candidate.entry_template in ("A", "B", "C")}
    values_match = all(
        candidate.ticker in reference
        and (candidate.entry_template, candidate.tss_final)
        == (reference[candidate.ticker].entry_template, reference[candidate.ticker].tss_final)
        for candidate in scored)

    def evaluate(candidates):
        ctx = {**common, "watchlist": deepcopy(list(candidates)),
               "portfolio_state": {"initial_cash": _ACCOUNT, "cash": _ACCOUNT,
                                   "positions": [], "pending": []}}
        risk.execute(ctx)
        return ([asdict(pick) for pick in ctx["picks"]], ctx.get("entry_budget"), ctx["action"],
                ctx.get("rejections", {}).get("candidates", []))

    covered, all_stocks = evaluate(scored), evaluate(reference.values())
    full_diagnostics = {row["ticker"]: row for row in all_stocks[3]}
    return {"full_passed_hard_filter": len(reference), "full_templates": sorted(expected),
            "missed": sorted(expected - actual), "unexpected": sorted(actual - expected),
            "retained_scores_and_templates_match": values_match,
            "flat_risk_and_retained_diagnostics_match": (covered[:3] == all_stocks[:3]
                and all(row == full_diagnostics.get(row["ticker"]) for row in covered[3])),
            "flat_approved": [pick["ticker"] for pick in covered[0]]}


def _position_concentration(book):
    values = [p["shares"] * p.get("last_price", p["entry_price"]) for p in book.state["positions"]]
    equity = book.state["cash"] + sum(values)
    return round(max(values, default=0.0) / equity, 6) if equity > 0 else None


def _by_regime(rows, books):
    families = {"黄金共振": "trend", "健康趋势": "trend", "震荡混乱": "mixed",
                "负向共振": "weak"}
    grouped = {key: [] for key in ("trend", "mixed", "weak", "unknown")}
    for row in rows:
        grouped[families.get(row.get("regime"), "unknown")].append(row)
    for family, subset in grouped.items():
        for row in subset:
            row["regime_group"] = family
    result = {}
    for family, subset in grouped.items():
        dates = {row["date"] for row in subset}
        expected = sum(row["eligible_symbols"] for row in subset)
        observed = sum(row["scan_symbols_with_exact_bar"] for row in subset)
        history_gaps = sum(len(row["insufficient_scan_history"]) for row in subset)
        portfolios = {}
        for mode, book in books.items():
            closed = book.state["closed"]
            exits = [item for item in closed if item["exit_date"] in dates]
            entries = sum(item["entry"] * item["shares"] for item in closed if item["entry_date"] in dates)
            entries += sum(item["entry_price"] * item["shares"] for item in book.state["positions"]
                           if item["entry_date"] in dates)
            turnover = entries + sum(item["exit"] * item["shares"] for item in exits)
            n = len(exits)
            portfolio_rows = [row["paper"][mode] for row in subset]
            portfolios[mode] = {
                "settled_trades": n, "net_realized_usd": round(sum(x["pnl_usd"] for x in exits), 2),
                "settled_friction_usd": round(sum(x.get("friction_cost", 0.0) for x in exits), 2),
                "mean_net_usd_per_settled": round(sum(x["pnl_usd"] for x in exits) / n, 2) if n else None,
                "mean_net_r_per_settled": round(sum(x["r_multiple"] for x in exits) / n, 6) if n else None,
                "turnover_initial_capital": round(turnover / book.initial_cash, 6),
                "max_drawdown_from_window_peak": max((x["drawdown"] for x in portfolio_rows), default=None),
                "max_single_position_share_of_equity": max(
                    (x["largest_position_equity_share"] for x in portfolio_rows
                     if x["largest_position_equity_share"] is not None), default=None),
                "open_positions_last_regime_session": portfolio_rows[-1]["open_positions"] if subset else None,
                "pending_signals_last_regime_session": portfolio_rows[-1]["pending_signals"] if subset else None,
                "sessions_with_unresolved_lifecycle_exposure": sum(
                    bool(item["unresolved_lifecycle_exposures"]) for item in portfolio_rows),
            }
        result[family] = {"sessions": len(subset), "eligible_stock_days": expected,
                          "observed_stock_days": observed, "missing_stock_days": expected - observed,
                          "insufficient_history_stock_days": history_gaps,
                          "scan_input_stock_days": observed - history_gaps,
                          "coverage": round(observed / expected, 6) if expected else None,
                          "portfolios": portfolios}
    return result


def _run_period(market, universe, dates, directory, *, roster=None, oracle=False):
    provider = _HistoricalProvider()
    spec = get_market("us")
    calendar_file = directory / "calendar.json"
    calendar_file.write_text('{"macro":{},"earnings":{}}\n')
    calendar = EventCalendar(str(calendar_file), strict=True)
    books = {mode: _ResearchBook(str(directory / mode), initial_cash=_ACCOUNT)
             for mode in ("legacy_shortlist", "entry_setup_coverage")}
    peaks = dict.fromkeys(books, _ACCOUNT)
    exposures = {mode: {} for mode in books}
    risk = RiskManagerAgent(provider, account_usd=_ACCOUNT)
    rows = []
    for date in dates:
        session = date.date()
        view, open_view = None, None
        if roster:
            cutoff = _scheduled_close(session)
            view = roster.on(session, cutoff)
            open_view = roster.on(session, cutoff.replace(hour=9, minute=30), require_coverage=False)
        today = list(view.symbols) if view else universe
        if len(today) > len(universe):
            raise ValueError("Dated roster expands the reviewed pool")
        md = _market_for_date(market, date, roster_view=view)
        session = session.isoformat()
        missing = sorted(set(today) - md["stock_ohlcv"].keys())
        session_data = {"date": session, "stocks_with_exact_bar": len(md["stock_ohlcv"]),
                        "etfs_with_exact_bar": len(md["sector_etfs"]), "eligible_symbols": len(today),
                        "scan_symbols_with_exact_bar": len(today) - len(missing),
                        "missing_scan_symbols": missing, "paper": {}}
        minimum_history = config.SCAN_MIN_HISTORY_DAYS * 0.6
        session_data["insufficient_scan_history"] = sorted(
            symbol for symbol in today
            if symbol in md["stock_ohlcv"] and len(md["stock_ohlcv"][symbol]) < minimum_history)
        if view:
            session_data["roster"] = {"symbols": view.symbols, "unavailable": view.unavailable,
                                      "source_refs": list(view.source_refs)}
        usable = len(today) - len(missing) - len(session_data["insufficient_scan_history"])
        ready = (usable > 0 and md["spy"] is not None and len(md["spy"]) >= 260
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
            UniverseScannerAgent(provider, today, include_entry_setups=True).execute(common)
            stats = common["scan_stats"]
            supplement = stats["entry_setup_supplement"]
            baseline = {c.ticker for c in common["watchlist"][:supplement["baseline_selected"]]}
            scored = TSSAgent(provider).execute(common)
            session_data.update(regime=mrs.regime, mrs=mrs.mrs_star)
            if oracle:
                session_data["oracle"] = _coverage_oracle(common, today, provider, scored, risk)
        else:
            session_data["reason"] = ("insufficient_aligned_benchmark_or_sector_data" if usable
                                      else "no_eligible_stock_with_sufficient_history")
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
            bound = exposures[mode]
            unresolved = set()

            def daily_bar(ticker):
                if view and (view.members.get(ticker) != bound.get(ticker)
                             or open_view.members.get(ticker) != bound.get(ticker)):
                    unresolved.add(ticker)
                    return None
                return _bar(md["stock_ohlcv"].get(ticker), date)

            if view:
                for position in book.state["positions"]:
                    ticker = position["ticker"]
                    if ticker not in view.members or view.members[ticker] != bound.get(ticker):
                        unresolved.add(ticker)
            book.step(session, result, daily_bar)
            if view:
                exposed = {item["ticker"] for kind in ("positions", "pending") for item in book.state[kind]}
                exposures[mode] = {ticker: bound.get(ticker, view.members.get(ticker)) for ticker in exposed}
            equity = book.state["equity_curve"][-1]["equity"]
            peaks[mode] = max(peaks[mode], equity)
            session_data["paper"][mode] = {
                "drawdown": round((peaks[mode] - equity) / peaks[mode], 6),
                "largest_position_equity_share": _position_concentration(book),
                "open_positions": len(book.state["positions"]), "pending_signals": len(book.state["pending"]),
                "unresolved_lifecycle_exposures": sorted(unresolved),
            }
        rows.append(session_data)
    return {"from": str(dates[0].date()), "through": str(dates[-1].date()),
            "scored_sessions": sum("regime" in row for row in rows), "sessions": rows,
            "portfolios": {mode: _summary(book) for mode, book in books.items()},
            "regimes": _by_regime(rows, books)}


def _point_in_time_dates(period, admission):
    start, end = period
    if start.year < 2024 or end.year > 2028:
        raise ValueError("Dated roster replay is outside the verified US cash session calendar")
    sessions = pd.DatetimeIndex([day for day in pd.date_range(start, end)
                                 if _scheduled_close(day.date()) is not None])
    return _period_dates(sessions, period, admission)


def compare(state, run_id, period_specs, *, roster=None, oracle=False):
    periods = _periods(period_specs)
    market, universe, admission, receipt_hash = _load(state, run_id)
    index = market["spy"].index
    windows = [_point_in_time_dates(period, admission) if roster else _period_dates(index, period, admission)
               for period in periods]
    with TemporaryDirectory(prefix="tiger-coverage-") as directory:
        root = Path(directory)
        results = [_run_period(market, universe, dates, root, roster=roster, oracle=oracle)
                   for dates in windows]
    all_rows = [row for result in results for row in result["sessions"]]
    oracle_rows = [row for row in all_rows if "oracle" in row]
    failures = [row["date"] for row in oracle_rows if (
        row["oracle"]["missed"] or row["oracle"]["unexpected"]
        or not row["oracle"]["retained_scores_and_templates_match"]
        or not row["oracle"]["flat_risk_and_retained_diagnostics_match"])]
    return {"schema": _SCHEMA, "source_run_id": run_id, "source_receipt_sha256": receipt_hash,
            "signal_universe_size": len(universe), "periods": results,
            "roster": ({"basis": "operator_pinned_bitemporal", "sha256": roster.digest,
                        "sources": roster.sources} if roster else {"basis": "frozen_current_pool"}),
            "oracle": ({"scored_sessions": len(oracle_rows), "minimum_sessions": 30,
                        "unscored_dates": [row["date"] for row in all_rows if "oracle" not in row],
                        "mismatch_dates": failures, "passed": len(oracle_rows) >= 30 and not failures}
                       if oracle else None),
            "limits": [
                "Vendor-adjusted prices may include later revisions; prices and eligibility are separate evidence.",
                "The fixed-pool comparator has survivorship bias." if roster is None else
                "Corporate actions quarantine old price epochs; unreconciled holdings retain their last mark.",
                "No historical option, semantic, candidate news or model gate inputs are fabricated.",
                "Two paper books start flat in each interval; costs, holdings and pending signals are reported.",
                "Exit costs use ADV through the session before execution.",
                "Regime settlement metrics group by exit date; turnover uses each transaction date.",
                "Numerical comparison only; it does not infer strategy efficacy or market-wide coverage."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--period", action="append", required=True)
    parser.add_argument("--roster", type=Path)
    parser.add_argument("--roster-sha256")
    parser.add_argument("--verify-oracle", action="store_true")
    args = parser.parse_args()
    if bool(args.roster) != bool(args.roster_sha256):
        parser.error("--roster and --roster-sha256 must be supplied together")
    try:
        roster = SecurityRoster.load(args.roster, args.roster_sha256) if args.roster else None
        report = compare(args.state_root, args.run_id, args.period, roster=roster, oracle=args.verify_oracle)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        if args.verify_oracle and not report["oracle"]["passed"]:
            parser.exit(1, "Full TSS oracle mismatch or fewer than 30 scored sessions.\n")
    except (EvidenceInvalid, OSError, ValueError) as exc:
        parser.exit(2, f"Coverage comparison unavailable: {exc}\n")


if __name__ == "__main__":
    main()
