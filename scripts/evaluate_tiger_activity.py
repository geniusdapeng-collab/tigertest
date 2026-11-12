#!/usr/bin/env python3
"""Offline retrospective comparison from a hash-bound strict-run snapshot.

This is research, never an account publication or a broker execution. Both
decision profiles use the same corrected signal-time-budgeted SimEngine fills.
The current universe/classification snapshot makes this unsuitable as clean OOS
profitability evidence. No network, model, tuning, or current news is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import socket
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system import config
from trading_system.activity import ACTIVE_PROFILE, assign_pools, risk_group
from trading_system.agent_runtime import executable_files
from trading_system.agents.chain_cycle_agent import ChainCycleAgent
from trading_system.agents.mrs_agent import MRSAgent
from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.agents.sector_agent import SectorAgent
from trading_system.agents.tss_agent import TSSAgent
from trading_system.agents.universe_scanner_agent import UniverseScannerAgent
from trading_system.data_models import PipelineResult
from trading_system.events import EventCalendar
from trading_system.indicators import breadth_above_sma
from trading_system.markets.us import USMarket
from trading_system.redline import Passthrough
from trading_system.simulator import Bar, SimEngine
from trading_system.tech_chain.agents import CycleLinkageAgent, TechChainMonitorAgent
from trading_system.tech_chain.fusion import TechChainFusionAgent, signals_to_bonus_map


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_ref(root: Path, ref: dict):
    path = (root / ref["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes run directory")
    raw = path.read_bytes()
    if len(raw) != ref["bytes"] or sha(raw) != ref["sha256"]:
        raise ValueError("Evidence hash/length mismatch: " + ref["path"])
    return json.loads(raw)


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("__type__")
    if kind == "Timestamp":
        # Financial day labels are local exchange dates, not UTC timestamps.
        return pd.Timestamp(value["value"][:10])
    if kind == "dataframe":
        return pd.DataFrame(decode(value["values"]), index=decode(value["index"]),
                            columns=value["columns"]).sort_index()
    if kind == "series":
        return pd.Series(decode(value["values"]), index=decode(value["index"]),
                         name=value.get("name"), dtype=float).sort_index()
    if kind in {"float", "nonfinite"}:
        return float(value["value"])
    return {k: decode(v) for k, v in value.items()}


def ad20_vectorized(closes: dict) -> float:
    arrays = [s.dropna().to_numpy(dtype=float) for s in closes.values()
              if len(s.dropna()) > 272]
    if len(arrays) < 30:
        return float("nan")
    rets = np.stack([a[-233:][::-1] / a[-253:-20][::-1] - 1 for a in arrays])
    up, down = (rets > 0).sum(axis=0), (rets < 0).sum(axis=0)
    total = up + down
    ratios = np.divide(up, total, out=np.full(233, 0.5), where=total > 0)
    return float(np.sum(ratios < ratios[0]) / len(ratios))


class FrozenProvider:
    name = "recorded-yahoo-retrospective"
    synthetic = False

    def __init__(self, frames):
        self.frames, self.day = frames, None

    def ohlcv(self, ticker, days=400):
        frame = self.frames.get(ticker)
        if frame is None:
            return None
        return frame.loc[:self.day].tail(days)

    def ohlcv_batch(self, tickers, days=400):
        return {t: f for t in tickers if (f := self.ohlcv(t, days)) is not None and len(f)}

    def gex_billions(self):
        return None

    def zero_dte_share(self):
        return None

    def options_chain_snapshot(self, ticker):
        return None


def blocked_network(*_args, **_kwargs):
    raise RuntimeError("Historical replay is offline; network use is forbidden")


def summarize(rows, initial, closed, final_state, fill_checks):
    nav = [initial] + [r["equity"] for r in rows]
    peak, dd = initial, 0.0
    for value in nav:
        peak = max(peak, value)
        dd = max(dd, 1 - value / peak)
    days = {r["date"] for r in rows}
    closes = [r for r in closed if r["exit_date"] in days]
    fills = [r for r in fill_checks if r["date"] in days and r["reason"] == "accepted"]
    closed_cost = sum(r.get("friction_cost", 0) for r in closes)
    # Marked NAV already includes fees for still-open positions; disclose them too.
    open_entry_cost = sum((p["entry_price"] - p["entry_raw"]) * p["shares"]
                         for p in final_state["positions"] if p["entry_date"] in days)
    return {"start": rows[0]["date"], "end": rows[-1]["date"], "sessions": len(rows),
        "starting_equity": round(initial, 2), "ending_equity": rows[-1]["equity"],
        "net_total_return": nav[-1] / initial - 1, "max_drawdown": dd,
        "buy_fills": len(fills), "sell_fills": len(closes), "round_trips": len(closes),
        "buy_fills_per_20_sessions": len(fills) / len(rows) * 20,
        "closed_win_rate": (sum(r["pnl_usd"] > 0 for r in closes) / len(closes) if closes else None),
        "days_in_market": sum(r["positions"] > 0 for r in rows),
        "average_close_gross": sum(r["gross"] for r in rows) / len(rows),
        "peak_close_gross": max(r["gross"] for r in rows),
        "peak_opening_entry_gross": max((r["gross_after"] for r in fills), default=0),
        "peak_opening_single_risk": max((r["risk_after"] for r in fills), default=0),
        "peak_opening_group_risk": max((r["group_risk_after"] for r in fills), default=0),
        "opening_entry_limit_violations": sum(r["entry_limit_violation"] for r in fills),
        "passive_close_gross_above_current_cap_days": sum(r["gross"] > r["current_cap"] + 1e-9 for r in rows),
        "passive_close_single_above_20pct_days": sum(r["max_single"] > config.MAX_SINGLE_POSITION_PCT + 1e-9 for r in rows),
        "passive_close_group_stop_risk_above_3pct_days": sum(r["max_group_risk"] > config.MAX_CHAIN_RISK_PCT + 1e-9 for r in rows),
        "costs_for_trades_closed_in_period_including_prior_entry": round(closed_cost, 4),
        "entry_costs_charged": round(sum(r["entry_cost"] for r in rows), 4),
        "exit_costs_charged": round(sum(r["exit_cost"] for r in rows), 4),
        "total_costs_charged": round(sum(r["entry_cost"] + r["exit_cost"] for r in rows), 4),
        "peak_positions_after_entry": max((r["positions_after"] for r in fills), default=0),
        "duplicate_entry_violations": sum(r["duplicate"] for r in fills),
        "open_position_entry_costs_at_final_mark": round(open_entry_cost, 4)}


def evaluate(run: Path, output: Path, warmup: int):
    started = time.monotonic()
    source_root = Path(__file__).resolve().parents[1]
    def source_hashes():
        hashes = {name: sha((source_root / name).read_bytes()) for name in executable_files(source_root)}
        hashes["scripts/evaluate_tiger_activity.py"] = sha(Path(__file__).read_bytes())
        return hashes
    code_hashes = source_hashes()
    events_bytes = (run / "events.jsonl").read_bytes()
    events = [json.loads(line) for line in events_bytes.splitlines()]
    prepare = next(e for e in events if e["stage"] == "data.prepare" and e["kind"] == "end")
    snapshot = load_ref(run, prepare["output_ref"])
    prepared = decode(snapshot["value"]["value"])
    market, universe = prepared["market_data"], prepared["universe"]
    frames = dict(market["stock_ohlcv"])
    frames["SPY"] = market["spy"]
    supplemental_refs = []
    for event in events:
        if event["kind"] != "end" or event["stage"] not in {"tech.monitor", "tech.cycle_linkage"}:
            continue
        stage = load_ref(run, event["output_ref"])
        for call in stage["value"]["io_calls"]:
            if not call["method"].endswith(".ohlcv"):
                continue
            inp = load_ref(run, call["input_ref"])["value"]
            value = decode(load_ref(run, call["output_ref"])["value"])
            ticker = inp["args"][0]
            if isinstance(value, pd.DataFrame) and ticker not in frames:
                frames[ticker] = value
                supplemental_refs += [call["input_ref"], call["output_ref"]]
    dates = list(market["spy"].index[warmup:])
    if len(dates) < 60:
        raise ValueError("Insufficient post-warmup sessions for three chronological segments")
    output.mkdir(parents=True, exist_ok=True)
    if any((output / (name + "-account.json")).exists() for name in ("legacy", "active")):
        raise ValueError("Replay output accounts already exist; choose a fresh output directory")
    provider, calendar = FrozenProvider(frames), EventCalendar(str(run / "work/events_calendar.json"))
    # All external effects are blocked after the frozen artifacts have been read.
    socket.socket = blocked_network
    socket.create_connection = blocked_network
    engines = {name: SimEngine(str(output / (name + "-account.json"))) for name in ("legacy", "active")}
    rows, checks, decisions = ({k: [] for k in engines} for _ in range(3))
    rejection_counts = {k: Counter() for k in engines}
    ad_checks = []
    checked = {dates[i] for i in (13, len(dates) // 2, len(dates) - 1)}
    for index, day in enumerate(dates):
        daystr = day.date().isoformat()
        provider.day = day
        data = {k: (v.loc[:day] if isinstance(v, (pd.Series, pd.DataFrame)) else v)
                for k, v in market.items() if k not in {"stock_ohlcv", "universe_closes", "sector_etfs", "freshness"}}
        stocks = {t: df.loc[:day] for t, df in market["stock_ohlcv"].items()}
        stocks = {t: df for t, df in stocks.items() if len(df) and (day - df.index[-1]).days <= 5}
        data.update(stock_ohlcv=stocks, universe_closes={t: df["Close"] for t, df in stocks.items()},
                    sector_etfs={t: df.loc[:day] for t, df in market["sector_etfs"].items()})
        if (len(data["spy"]) < 260 or len(data["tnx"]) < 100 or len(data["vix"]) < 30
                or len(data["sector_etfs"]) < 8):
            raise ValueError("Historical production data-quality gate failed on " + daystr)
        ad_q = ad20_vectorized(data["universe_closes"])
        if day in checked:
            reference = MRSAgent._ad20_quantile(data["universe_closes"])
            same = (np.isnan(ad_q) and np.isnan(reference)) or ad_q == reference
            ad_checks.append({"date": daystr, "vectorized": ad_q, "production": reference, "equal": same})
            if not same:
                raise AssertionError("AD20 vectorization differs from production")
        data["precomputed"] = {"ad20_q": ad_q, "breadth200": breadth_above_sma(data["universe_closes"], 200)}
        context = {"market_data": data, "trade_date": daystr, "market_spec": USMarket(),
                   "event_calendar": calendar, "narrative_llm": {}}
        monitor = TechChainMonitorAgent(provider).execute(prefetched=stocks)
        linkage = CycleLinkageAgent(provider).execute()
        unavailable = Passthrough([], "historical", "No historical news/model observations")
        tech = TechChainFusionAgent().execute(monitor, linkage, unavailable, unavailable)
        context.update(tech_bonus_map=signals_to_bonus_map(tech), tech_signal_map={s.main_chain: s for s in tech})
        mrs = MRSAgent(provider).execute(context)
        SectorAgent(provider).execute(context)
        ChainCycleAgent(provider).execute(context)
        UniverseScannerAgent(provider, universe).execute(context)
        TSSAgent(provider).execute(context)
        for name, engine in engines.items():
            ctx = deepcopy(context)
            ctx.update(paper_execution_budgeted=True, portfolio_state=deepcopy(engine.state))
            if name == "active":
                ctx.update(activity_profile=ACTIVE_PROFILE)
                assign_pools(ctx["sectors"], active=True)
            picks = RiskManagerAgent(provider, account_usd=100000).execute(ctx)
            rejection_counts[name].update(ctx["rejections"]["overlapping_reason_counts"])
            # Same execution safety for both profiles; experimental sector gates remain separate.
            for pick in picks:
                ctx["pick_rationale"][pick.ticker]["signal_adv_usd"] = next(
                    c.adv_usd for c in ctx["watchlist"] if c.ticker == pick.ticker)
            budget = ctx.get("entry_budget") or {"schema": "tiger.entry-budget.v1",
                "gross_cap": ctx.get("gross_cap", mrs.position_cap[1])}
            result = PipelineResult(trade_date=daystr, provider=provider.name, mrs=mrs,
                action=ctx["action"], picks=picks, raw={"activity_profile": ACTIVE_PROFILE if name == "active" else None,
                "paper_execution_budgeted": True, "paper_execution_version": "signal-budgeted-open-v1",
                "entry_budget": budget, "pick_rationale": ctx.get("pick_rationale", {}), "account_usd": 100000})
            def get_bar(ticker):
                frame = stocks.get(ticker)
                if frame is None:
                    return None
                bar = frame.iloc[-1]
                adv = float((frame["Close"] * frame["Volume"]).tail(20).mean())
                return Bar(float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
                           adv=adv, date=frame.index[-1].date().isoformat())
            before_closed = len(engine.state["closed"])
            out = engine.step(daystr, result, get_bar)
            for fill in out["fill_checks"]:
                if fill["reason"] == "accepted":
                    b = fill["budget"]
                    fill["entry_limit_violation"] = bool(
                        fill["gross_after"] > b["gross_cap"] + 1e-9
                        or fill["single_after"] > b["single_cap"] + 1e-9
                        or fill["risk_after"] > b["risk_pct"] + 1e-9
                        or fill["group_risk_after"] > b["group_risk_pct"] + 1e-9
                        or fill["positions_after"] > b["max_positions"] or fill["duplicate"])
                checks[name].append(fill)
            state, equity = engine.state, out["equity"]
            values = [p["shares"] * p.get("last_price", p["entry_price"]) for p in state["positions"]]
            groups = {}
            for p in state["positions"]:
                group = p.get("risk_group") or risk_group(p.get("chain", ""), p.get("sector", ""))
                groups[group] = groups.get(group, 0) + p["shares"] * max(0, p["entry_price"] - p["stop"])
            rows[name].append({"date": daystr, "equity": equity,
                "cash": state["cash"], "positions": len(state["positions"]), "pending": len(state["pending"]),
                "gross": sum(values) / equity, "max_single": max(values, default=0) / equity,
                "max_group_risk": max(groups.values(), default=0) / equity,
                "current_cap": mrs.position_cap[1] * calendar.day_discount(daystr)[0],
                "stock_coverage": f"{len(stocks)}/{len(prepared['all_tickers'])}",
                "entry_cost": sum(f["entry_friction_cost"] for f in out["fill_checks"] if f["reason"] == "accepted"),
                "exit_cost": sum(c["exit_friction_cost"] for c in state["closed"][before_closed:]),
                "signal_count": len(picks), "sell_fills": len(state["closed"]) - before_closed})
            decisions[name].append({"date": daystr, "mrs": asdict(mrs), "action": ctx["action"],
                "picks": [asdict(p) for p in picks], "rejections": ctx["rejections"],
                "sectors": [asdict(s) for s in ctx["sectors"]], "tech_signals": [s.to_dict() for s in tech]})
        if index % 10 == 0 or index == len(dates) - 1:
            print(json.dumps({"progress": index + 1, "sessions": len(dates), "date": daystr,
                              "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
    if source_hashes() != code_hashes:
        raise RuntimeError("Source changed during replay; no successful evaluation is published")
    report = {"schema": "tiger.activity-evaluation.v1", "run_id": run.name,
        "data_snapshot": prepare["output_ref"], "events_sha256": sha(events_bytes),
        "supplemental_refs": supplemental_refs, "source_file_sha256": code_hashes,
        "method": {"network": "blocked", "model": "disabled", "tuning": False,
            "warmup_sessions": warmup, "signal_account_usd": 100000,
            "paper_execution_version": "signal-budgeted-open-v1", "recognized_entry_setup_required": True,
            "no_chase_guard": "Known sector breadth below 40: signal and next open cannot exceed signal key high",
            "execution": "same actual SimEngine with frozen prior-signal budget and opening NAV",
            "continuous_account": True, "segment_resets": False, "source_unchanged_after_replay": True,
            "price_index_normalization": "exchange-local calendar date; TNX 01:00 retained in same day",
            "AD20_reference_checks": ad_checks},
        "limitations": ["Current universe and current frozen vendor sector proxies introduce survivorship/classification lookahead; not clean OOS evidence.",
            "Recorded adjusted daily bars are a retrospective vendor snapshot, not contemporaneously archived observations.",
            "No historical options chain, news, earnings overlay or model output; missing dimensions are excluded by the existing production rules.",
            "Global leadership supplements only cover recorded dates; earlier dates honestly omit unavailable observations.",
            "Both decision profiles use the corrected common full-account reservation and executor; this does not reproduce historical execution defects.",
            "Signal plans use fixed 100000 USD like production; opening allocation uses actual cash and opening holdings NAV.",
            "Stops can gap; passive mark-to-market breaches are disclosed separately from new-entry violations.",
            "Exit-day friction is an end-of-day turnover estimate; opening tiers are frozen at signal time.",
            "Return includes unrealized marked positions; final positions are not forcibly liquidated."], "profiles": {}}
    cuts = np.array_split(np.arange(len(dates)), 3)
    for name, engine in engines.items():
        series = rows[name]
        summary = summarize(series, 100000, engine.state["closed"], engine.state, checks[name])
        segments = []
        for chunk in cuts:
            previous = 100000 if chunk[0] == 0 else series[int(chunk[0]) - 1]["equity"]
            segments.append(summarize([series[int(i)] for i in chunk], previous,
                                     engine.state["closed"], engine.state, checks[name]))
        summary["signals"] = sum(r["signal_count"] for r in series)
        report["profiles"][name] = {"summary": summary, "segments": segments,
            "denial_counts": dict(rejection_counts[name]), "daily": series, "fills": checks[name]}
        (output / (name + "-decisions.json")).write_text(json.dumps(decisions[name], ensure_ascii=False, allow_nan=False))
    benchmark = market["spy"]["Close"]
    report["benchmark"] = {"name": "SPY adjusted close, no trading costs",
        "start_previous_close": str(benchmark.index[warmup - 1].date()), "end": str(dates[-1].date()),
        "return": float(benchmark.loc[dates[-1]] / benchmark.iloc[warmup - 1] - 1)}
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    (output / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({"output": str(output / "evaluation.json"),
        "profiles": {k: v["summary"] for k, v in report["profiles"].items()}}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--warmup", default=260, type=int)
    args = parser.parse_args()
    if args.warmup < 260:
        parser.error("At least 260 warmup sessions are required")
    logging.basicConfig(level=logging.ERROR)
    evaluate(args.run_dir.resolve(), args.output_dir.resolve(), args.warmup)


if __name__ == "__main__":
    main()
