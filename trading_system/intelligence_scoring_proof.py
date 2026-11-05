"""Read-only replay of every deterministic v2 scoring/approval stage.

The parent verifier first verifies source parsers, typed annotations, semantic
receipts and fusion. This helper reconstructs market frames, the admitted
portfolio/calendar and narrowly observed metric/option inputs, then runs the
ordinary rule agents in order. Stored score/shortlist/approval outputs are only
comparison targets. No provider factory, model, transport or ledger write runs.
"""
from __future__ import annotations

import copy
from dataclasses import fields
from datetime import date
import math
from pathlib import Path
import re

import pandas as pd

from . import config
from .agent_evidence import (EvidenceInvalid, normalize, read_bytes, read_json,
                             read_snapshot, strict_json, verify_ref)
from .agents.chain_cycle_agent import ChainCycleAgent
from .agents.mrs_agent import MRSAgent
from .agents.risk_manager_agent import RiskManagerAgent
from .agents.sector_agent import SectorAgent
from .agents.tss_agent import TSSAgent
from .agents.universe_scanner_agent import UniverseScannerAgent
from .events import EventCalendar, _CPI, _FOMC
from .intelligence_workflow import qualify_candidates, restore_frame
from .market_close import _snapshot_window, requires_foreign_calendar, verify_close_evidence
from .markets import get_market
from .options_metrics import OptionsHistoryStore
from .redline.execution import ExecutionContractError
from .tech_chain.agents import CycleLinkageAgent, TechChainMonitorAgent
from .tech_chain.fusion import StructuredTechChainSignal, signals_to_bonus_map
from .tech_chain.universe import TECH_SUBCHAINS


CONTRACT = "tiger.intelligence-scoring-proof/v1"
STAGES = ("tech.monitor", "tech.cycle_linkage", "layer1.mrs", "layer2.sector",
          "layer2b.chain", "layer0.scan", "layer3.tss", "layer4.risk")


def _fail(stage=None):
    raise EvidenceInvalid("MODEL_REQUIRED_FAILED", "Frozen deterministic intelligence scoring cannot be reproduced", stage=stage)


def _same(actual, expected, stage=None):
    if normalize(actual) != normalize(expected):
        _fail(stage)


def _unpack(value):
    """Restore finite JSON and declared missing scalars, never arbitrary objects."""
    if isinstance(value, dict):
        if value.get("__type__") == "missing":
            if set(value) != {"__type__", "reason"} or value["reason"] not in {
                    "nan", "pandas_na", "positive_infinity", "negative_infinity"}:
                raise ExecutionContractError("Invalid frozen missing scalar")
            return (float("inf") if value["reason"] == "positive_infinity" else
                    float("-inf") if value["reason"] == "negative_infinity" else float("nan"))
        if "__type__" in value:
            raise ExecutionContractError("Unexpected frozen external value type")
        return {key: _unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unpack(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ExecutionContractError("Unsupported frozen external value")


def _series(packet):
    if packet is None:
        return None
    if (not isinstance(packet, dict) or set(packet) != {
            "__type__", "name", "index", "index_names", "dtype", "values"}
            or packet["__type__"] != "series"
            or len(packet["index"]) != len(packet["values"])):
        raise ExecutionContractError("Invalid frozen numeric series")
    labels = []
    for label in packet["index"]:
        if not isinstance(label, dict) or set(label) != {"__type__", "value"} or label["__type__"] != "Timestamp":
            raise ExecutionContractError("Invalid frozen numeric index")
        labels.append(pd.Timestamp(label["value"]))
    values = _unpack(packet["values"])
    if any(type(value) not in {int, float} for value in values):
        raise ExecutionContractError("Non-numeric frozen series")
    result = pd.Series(values, index=pd.DatetimeIndex(labels), name=packet["name"], dtype=packet["dtype"])
    result.index.names = packet["index_names"]
    _same(packet, result, "data.prepare")
    return result


def _market(packet):
    if set(packet) != {"benchmark_labels", "spy", "tnx", "vix", "vix9d", "sector_etfs",
                       "stock_ohlcv", "freshness", "universe_closes"}:
        raise ExecutionContractError("Unreviewed numeric market fields")
    result = {"benchmark_labels": packet["benchmark_labels"], "spy": restore_frame(packet["spy"]),
              "tnx": _series(packet["tnx"]), "vix": _series(packet["vix"]), "vix9d": _series(packet["vix9d"]),
              "sector_etfs": {key: restore_frame(value) for key, value in packet["sector_etfs"].items()},
              "stock_ohlcv": {key: restore_frame(value) for key, value in packet["stock_ohlcv"].items()},
              "freshness": packet["freshness"]}
    result["universe_closes"] = {key: frame["Close"] for key, frame in result["stock_ohlcv"].items()}
    _same(packet, result, "data.prepare")
    return result


def _sector_order(packet, market_input, io, day):
    """Recover stable tie order from actual batch requests, not JSON key order.

    Runtime fallback appends each source's accepted symbols in request order.
    Canonical JSON sorts mappings, which otherwise changes stable SHS ties and
    mainline slot selection during replay. Match each symbol to the first raw
    return that reconstructs its final accepted frame, preserving that request's
    explicit ordered symbol array.
    """
    selected = []
    cutoff = date.fromisoformat(market_input["market_close"]["session"])
    for call, request, response in io:
        if not call["method"].startswith("provider.") or not call["method"].endswith(".ohlcv_batch") or "error_ref" in call:
            continue
        if request["kwargs"].get("days") != market_input["days"] or not isinstance(response, dict):
            continue
        for symbol in request["args"][0]:
            if symbol in selected or symbol not in packet["sector_etfs"]:
                continue
            rebuilt = _snapshot_window(response.get(symbol), cutoff, day, exact=True)
            if rebuilt == packet["sector_etfs"][symbol]:
                selected.append(symbol)
    if set(selected) != set(packet["sector_etfs"]):
        raise ExecutionContractError("Sector order has no matching observed batch")
    return selected


def _signal(packet):
    cls = StructuredTechChainSignal
    if (not isinstance(packet, dict) or set(packet) != {"__type__", "class", "fields"}
            or packet["__type__"] != "dataclass" or packet["class"] != cls.__module__ + "." + cls.__qualname__
            or set(packet["fields"]) != {field.name for field in fields(cls)}):
        raise ExecutionContractError("V2 scoring requires verified structured fusion")
    result = cls(**_unpack(packet["fields"]))
    _same(packet, result, "tech.fusion")
    return result


def _calendar(text, original_path):
    overlay = strict_json(text)
    if not isinstance(overlay, dict) or set(overlay) - {"macro", "earnings"}:
        raise ExecutionContractError("Invalid frozen calendar overlay")
    for group in ("macro", "earnings"):
        values = overlay.get(group, {})
        if not isinstance(values, dict):
            raise ExecutionContractError("Invalid frozen calendar entries")
        for key, value in values.items():
            if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
                raise ExecutionContractError("Invalid frozen calendar value")
            day = key if group == "macro" else value
            if date.fromisoformat(day).isoformat() != day:
                raise ExecutionContractError("Invalid frozen calendar day")
    # Build exactly the normal EventCalendar state from sealed bytes, without
    # reading an ambient env path or changing the recorded deployment location.
    result = object.__new__(EventCalendar)
    result._macro = {**{day: "FOMC" for day in _FOMC}, **{day: "CPI" for day in _CPI}, **overlay.get("macro", {})}
    result._earnings = {key.upper(): value for key, value in overlay.get("earnings", {}).items()}
    result.override_path = original_path
    return result


class _ObservedProviderFailure(RuntimeError):
    """A frozen external failure replayed without contacting that provider."""


class _Trace:
    """A fully loaded I/O tape; mismatch errors cannot be swallowed as fallback."""
    def __init__(self, calls):
        self.calls, self.cursor = calls, 0

    def packet(self, method, inputs):
        if self.cursor >= len(self.calls):
            raise ExecutionContractError("Numeric replay attempted unobserved external I/O")
        call, recorded_input, output = self.calls[self.cursor]
        if call["method"] != method or normalize(inputs) != recorded_input:
            raise ExecutionContractError("Numeric replay I/O order or input differs")
        self.cursor += 1
        if "error_ref" in call:
            raise _ObservedProviderFailure("Frozen external call failed")
        return copy.deepcopy(output)

    def take(self, method, inputs):
        return _unpack(self.packet(method, inputs))

    def finish(self):
        if self.cursor != len(self.calls):
            raise ExecutionContractError("Numeric replay left unused external evidence")


class _Provider:
    synthetic = False

    def __init__(self, name, trace):
        self.name, self.trace = name, trace

    def __getattr__(self, method):
        if method not in {"gex_billions", "zero_dte_share", "options_chain_snapshot"}:
            raise ExecutionContractError("Numeric replay cannot construct or query a provider")
        def invoke(*args, **kwargs):
            return self.trace.take("provider." + self.name + "." + method,
                {"provider": self.name, "method": method, "args": list(args), "kwargs": kwargs})
        return invoke


class _NoProvider:
    def __getattr__(self, name):
        raise ExecutionContractError("Momentum replay attempted an unobserved provider call")


def _observed_frame(packet, session, day):
    rebuilt = _snapshot_window(packet, session, day, exact=True)
    return restore_frame(rebuilt) if rebuilt is not None else None


def _monitor(market, tape, provider, session, day, excluded):
    """Rebuild monitor data from its exact admitted batch/retry observations.

    The real 45-second retry loop may have observed only a prefix of individual
    retries. That prefix supplies facts; the unobserved suffix stays absent.
    Numeric aggregation is run by the ordinary agent over reconstructed frames,
    with absent symbols excluded from transport dispatch only.
    """
    tickers = sorted({ticker for chain in TECH_SUBCHAINS.values()
                      for members in chain["links"].values() for ticker in members} - excluded)
    data = {ticker: market["stock_ohlcv"][ticker] for ticker in tickers
            if ticker in market["stock_ohlcv"] and len(market["stock_ohlcv"][ticker]) >= 25}
    missing = [ticker for ticker in tickers if ticker not in data]
    if missing:
        try:
            extra = tape.packet("provider." + provider + ".ohlcv_batch",
                {"provider": provider, "method": "ohlcv_batch", "args": [missing], "kwargs": {"days": 90}})
        except _ObservedProviderFailure:
            for ticker in missing:
                if tape.cursor == len(tape.calls):
                    break
                try:
                    packet = tape.packet("provider." + provider + ".ohlcv",
                        {"provider": provider, "method": "ohlcv", "args": [ticker], "kwargs": {"days": 90}})
                except _ObservedProviderFailure:
                    continue
                frame = _observed_frame(packet, session, day)
                if frame is not None:
                    data[ticker] = frame
        else:
            if not isinstance(extra, dict) or set(extra) - set(missing):
                raise ExecutionContractError("Monitor batch returned unrequested securities")
            for ticker, packet in extra.items():
                frame = _observed_frame(packet, session, day)
                if frame is not None:
                    data[ticker] = frame
    tape.finish()
    unavailable = {ticker for ticker in tickers if ticker not in data or len(data[ticker]) < 25}
    return TechChainMonitorAgent(_NoProvider()).execute(prefetched=data,
                                                       excluded=frozenset(excluded | unavailable))


def _linkage(market, tape, provider, allowed_providers, session, day):
    """Recompute global momentum over prefetched or separately observed closes.

    Source fallback order is an observed acquisition fact. Every consumed call
    must be an admitted provider availability probe or that leader's exact
    90-day OHLCV request; only the first usable completed-session return can
    supply its frame. No stored momentum/relative-strength output is an input.
    """
    prefetched = {"SPY": market["spy"], **{symbol: market["stock_ohlcv"][symbol]
        for symbol in CycleLinkageAgent.LEADERS if symbol in market["stock_ohlcv"]}}

    def fetch(symbol):
        if requires_foreign_calendar(symbol):
            return None
        first = True
        while tape.cursor < len(tape.calls):
            call, request, _ = tape.calls[tape.cursor]
            match = re.fullmatch(r"provider\.([a-z]+)\.(available|ohlcv)", call["method"])
            if match is None or match[1] not in allowed_providers:
                raise ExecutionContractError("Unexpected linkage provider receipt")
            source, method = match.groups()
            if method == "available":
                if first:
                    raise ExecutionContractError("Linkage omitted its primary provider observation")
                try:
                    available = tape.take(call["method"], {"provider": source})
                except _ObservedProviderFailure:
                    continue
                if type(available) is not bool:
                    raise ExecutionContractError("Invalid provider availability evidence")
                continue
            if request.get("args") != [symbol]:
                break
            if first and source != provider:
                raise ExecutionContractError("Linkage primary source differs from admission")
            first = False
            try:
                packet = tape.packet(call["method"], {"provider": source, "method": "ohlcv",
                    "args": [symbol], "kwargs": {"days": 90}})
            except _ObservedProviderFailure:
                continue
            frame = _observed_frame(packet, session, day)
            if frame is not None:
                return frame
        return None

    rows = CycleLinkageAgent(_NoProvider()).execute(prefetched=prefetched, fetch=fetch)
    tape.finish()
    return rows


def _history(value):
    if not isinstance(value, list):
        raise ExecutionContractError("Frozen options history must be a list")
    previous = None
    for record in value:
        if not isinstance(record, dict) or not {"date", "pcr_oi", "atm_iv", "call_oi"} <= set(record):
            raise ExecutionContractError("Invalid frozen options history")
        day = date.fromisoformat(record["date"])
        if day.isoformat() != record["date"] or previous is not None and day <= previous:
            raise ExecutionContractError("Ambiguous frozen options history day")
        previous = day
        if any(record[key] is not None and (type(record[key]) not in {int, float}
                or not math.isfinite(record[key])) for key in ("pcr_oi", "atm_iv", "call_oi")):
            raise ExecutionContractError("Invalid frozen options history value")
    return value


class _OptionsIO:
    def __init__(self, trace, run, account_manifest):
        self.trace, self.run, self.account_manifest = trace, run, account_manifest
        self.state = {}

    def call_io(self, method, inputs, callback):
        # callback is deliberately never executed: it contains the real writer.
        if method not in {"options.history.read", "options.history.write"}:
            raise ExecutionContractError("Numeric replay refused an unexpected ledger operation")
        actual = self.trace.take(method, inputs)
        ticker = inputs["ticker"]
        if method == "options.history.read":
            actual = _history(actual)
            if ticker in self.state:
                expected = self.state[ticker]
            elif self.account_manifest is not None:
                relative = "options_hist/" + ticker + ".json"
                refs = [ref for ref in self.account_manifest["files"] if ref["path"] == relative]
                if len(refs) > 1:
                    raise ExecutionContractError("Duplicate admitted options input")
                expected = _history(strict_json(verify_ref(self.run / "account-before", refs[0]))) if refs else []
            else:
                # Unit integration fixtures omit an account commitment. The
                # receipt explicitly reports this weaker input basis below.
                expected = actual
            if normalize(actual) != normalize(expected):
                raise ExecutionContractError("Options read differs from admitted before-state")
            self.state[ticker] = copy.deepcopy(actual)
        else:
            if normalize(actual) != normalize(inputs["records"]):
                raise ExecutionContractError("Options write receipt differs from the computed append")
            _history(actual)
            self.state[ticker] = copy.deepcopy(actual)
        return actual


class _OptionsStore(OptionsHistoryStore):
    def _path(self, ticker):
        if not isinstance(ticker, str) or re.fullmatch(r"[A-Za-z0-9.^-]{1,64}", ticker) is None:
            raise ExecutionContractError("Invalid frozen options ticker")
        # This is metadata for tape comparison, never a file opened by replay.
        return self.root / (ticker + ".json")


def verify_intelligence_scoring(run, stages, admission):
    """Replay eight actual rule stages from verified snapshots, without mutation.

    ``stages`` is the parent verifier's already checked end-event mapping. The
    helper rechecks every referenced snapshot and binds its mapping to the same
    event stream. Source/model/fusion validity remains the parent's obligation.
    """
    stage = None
    try:
        run = Path(run)
        recorded = {row["stage"]: row for line in read_bytes(run, "events.jsonl").splitlines()
                    if (row := strict_json(line))["kind"] == "end"}
        if recorded != stages:
            _fail()

        def snapshot(name, role):
            return read_snapshot(run, stages[name][role + "_ref"], run_id=run.name, stage=name, role=role)

        def output(name):
            return snapshot(name, "output")["value"]

        def trace(name):
            return _Trace([(call, read_snapshot(run, call["input_ref"], run_id=run.name, stage=name, role="input"),
                           read_snapshot(run, call.get("output_ref", call.get("error_ref")), run_id=run.name,
                                         stage=name, role="output"))
                           for call in snapshot(name, "output")["io_calls"]])

        clock = admission["clock"]
        day = date.fromisoformat(clock["run_date"])
        preflight_input, preflight = snapshot("daily.preflight", "input"), output("daily.preflight")
        prepared_input, prepared = snapshot("pipeline.prepare", "input"), output("pipeline.prepare")
        request, paths = preflight_input["request"], preflight_input["paths"]
        market_output = output("data.prepare")
        verify_close_evidence(run, admission, market_output["market_data"]["freshness"])
        market = _market(market_output["market_data"])
        sector_order = _sector_order(market_output["market_data"], snapshot("data.prepare", "input"),
                                    trace("data.prepare").calls, day)
        market["sector_etfs"] = {symbol: market["sector_etfs"][symbol] for symbol in sector_order}
        spec = get_market("us")
        if (request["market"] != "us" or request["demo"] is not False or request["use_tuned"] is not False
                or prepared_input["market"] != "us" or prepared_input["use_tuned"] is not False
                or type(request["top"]) is not int or not 1 <= request["top"] <= 1000
                or type(request["picks"]) is not int or not 1 <= request["picks"] <= config.MAX_PICKS_DEFAULT
                or type(request["account"]) not in {int, float} or not math.isfinite(request["account"]) or request["account"] <= 0
                or prepared_input["top_n"] != request["top"] or prepared_input["max_picks"] != request["picks"]
                or prepared_input["account_usd"] != request["account"]
                or prepared["trade_date"] != day.isoformat() or preflight["synthetic"] is not False):
            _fail()
        _same(prepared["market"], spec.to_dict(), "pipeline.prepare")
        _same(prepared["scan_filters"], spec.scan_filters(), "pipeline.prepare")
        _same(prepared["market_grad"], spec.graduation(), "pipeline.prepare")
        _same(prepared_input["portfolio_state"], preflight["sim"], "pipeline.prepare")
        _same(market["benchmark_labels"], {key: spec.benchmarks[key + "_label"] for key in ("index", "rate", "vol")}, "data.prepare")
        for key in ("options_history_dir", "calendar_override_path"):
            if not isinstance(paths[key], str) or not Path(paths[key]).is_absolute():
                _fail()
        calendar_text = read_bytes(run, "work/events_calendar.json").decode("utf-8")
        _same(preflight["calendar"], {"path": paths["calendar_override_path"], "text": calendar_text}, "daily.preflight")
        calendar = _calendar(calendar_text, paths["calendar_override_path"])
        _same(prepared["calendar"], calendar.to_dict(), "pipeline.prepare")
        manifest = read_json(run, "account-input.json") if (run / "account-input.json").exists() else None
        if manifest is not None:
            if manifest.get("schema") != "tiger.account-input.v1" or not isinstance(manifest.get("files"), list):
                _fail()
            refs = [ref for ref in manifest["files"] if ref["path"] == "sim_portfolio.json"]
            if len(refs) != 1:
                _fail()
            _same(preflight["sim"], strict_json(verify_ref(run / "account-before", refs[0])), "daily.preflight")
        provider_name = prepared["provider"]
        allowed_providers = prepared_input["policy"]["fields"]["provider_names"]
        if provider_name not in allowed_providers:
            _fail()
        close_binding = snapshot("data.prepare", "input")["market_close"]
        session = date.fromisoformat(close_binding["session"])
        excluded = frozenset(ticker for chain in TECH_SUBCHAINS.values()
            for members in chain["links"].values() for ticker in members if requires_foreign_calendar(ticker))
        stage = "tech.monitor"
        _same(snapshot(stage, "input"), {"prefetched": market["stock_ohlcv"], "provider": provider_name,
            "market_close": close_binding, "excluded": sorted(excluded)}, stage)
        _same(output(stage), _monitor(market, trace(stage), provider_name, session, day, excluded), stage)
        stage = "tech.cycle_linkage"
        prefetched = {"SPY", *(symbol for symbol in CycleLinkageAgent.LEADERS if symbol in market["stock_ohlcv"])}
        _same(snapshot(stage, "input"), {"leaders": CycleLinkageAgent.LEADERS, "provider": provider_name,
            "market_close": close_binding, "prefetched": sorted(prefetched)}, stage)
        _same(output(stage), _linkage(market, trace(stage), provider_name, allowed_providers, session, day), stage)
        narrative = output("sector.narrative")["narrative"]
        signals = [_signal(row) for row in output("tech.fusion")]
        signal_map = {signal.main_chain: signal for signal in signals}
        context = {"market_data": market, "trade_date": day.isoformat(), "market_spec": spec,
            "scan_filters": spec.scan_filters(), "market_grad": spec.graduation(),
            "event_calendar": calendar, "paper_execution_budgeted": True,
            "portfolio_state": copy.deepcopy(preflight["sim"]), "capture_scan_decisions": True,
            "narrative_llm": narrative, "tech_bonus_map": signals_to_bonus_map(signals), "tech_signal_map": signal_map}
        stage = "layer1.mrs"
        tape = trace(stage)
        provider = _Provider(provider_name, tape)
        _same(snapshot(stage, "input"), {"market_data": market}, stage)
        mrs = MRSAgent(provider).execute(context)
        tape.finish()
        _same(output(stage), mrs, stage)
        stage = "layer2.sector"
        tape = trace(stage)
        _same(snapshot(stage, "input"), {"market_data": market, "narrative": narrative}, stage)
        sectors = SectorAgent(provider).execute(context)
        tape.finish()
        _same(output(stage), {"sectors": sectors, "sector_map": context["sector_map"]}, stage)
        stage = "layer2b.chain"
        tape = trace(stage)
        _same(snapshot(stage, "input"), {"spy": market["spy"], "stock_ohlcv": market["stock_ohlcv"]}, stage)
        chains = ChainCycleAgent(provider).execute(context)
        tape.finish()
        _same(output(stage), {"chains": chains, "chain_map": context["chain_map"]}, stage)
        stage = "layer0.scan"
        tape = trace(stage)
        universe = market_output["universe"]
        _same(snapshot(stage, "input"), {"universe": universe, "top_n": request["top"], "market_data": market,
            "scan_filters": spec.scan_filters(), "sectors": sectors, "include_entry_setups": True}, stage)
        UniverseScannerAgent(provider, universe, top_n=request["top"], include_entry_setups=True).execute(context)
        exclusions = qualify_candidates(context, output("intelligence.frontier"))
        for row in exclusions:
            audit = context["scan_decisions"][row["symbol"]]
            audit["market_selection"], audit["selection"] = audit.get("selection"), "research_missing"
        observed = output(stage)
        elapsed = observed.get("scan_elapsed_ms")
        if type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0:
            _fail(stage)
        _same(observed, {"candidates": context["watchlist"], "scan_stats": context["scan_stats"],
            "scan_decisions": context["scan_decisions"], "scan_elapsed_ms": elapsed}, stage)
        tape.finish()
        stage = "layer3.tss"
        tape = trace(stage)
        provider.trace = tape
        store = _OptionsStore(paths["options_history_dir"], strict=True, as_of_date=day,
                              execution=_OptionsIO(tape, run, manifest))
        context["options_store"] = store
        _same(snapshot(stage, "input"), {"watchlist": context["watchlist"], "market_data": market,
            "chain_map": context["chain_map"], "sector_map": context["sector_map"],
            "tech_bonus_map": context["tech_bonus_map"], "tech_signal_map": signal_map, "trade_date": day.isoformat()}, stage)
        watchlist = TSSAgent(provider).execute(context)
        tape.finish()
        _same(output(stage), {"watchlist": watchlist, "options_history": store.observed}, stage)
        stage = "layer4.risk"
        tape = trace(stage)
        provider.trace = tape
        _same(snapshot(stage, "input"), {"watchlist": watchlist, "mrs": mrs, "sectors": sectors, "chains": chains,
            "market_grad": spec.graduation(), "market": spec.to_dict(), "trade_date": day.isoformat(),
            "account_usd": request["account"], "max_picks": request["picks"], "calendar": calendar.to_dict(),
            "tech_signal_map": signal_map}, stage)
        picks = RiskManagerAgent(provider, account_usd=request["account"], max_picks=request["picks"]).execute(context)
        tape.finish()
        _same(output(stage), {key: context.get(key) for key in ("picks", "pick_rationale", "action", "market_view",
              "notes", "currency_policy", "compliance", "rejections", "entry_budget")}, stage)
        result = output("report.emit")
        if result.get("__type__") != "dataclass":
            _fail("report.emit")
        _same(result["fields"]["watchlist"], context["watchlist"], "report.emit")
        _same(result["fields"]["raw"]["intelligence"]["exclusions"], exclusions, "report.emit")
        return {"contract": CONTRACT, "stages": list(STAGES), "market_basis": "verified_raw_provider_observations",
            "account_input_bound": manifest is not None, "universe_size": len(universe),
            "researched_candidates": len(watchlist), "numeric_approved": [pick.ticker for pick in picks],
            "external_inputs_replayed": True, "ledger_writes": 0, "network_requests": 0}
    except EvidenceInvalid:
        raise
    except (ArithmeticError, AttributeError, KeyError, IndexError, OSError, TypeError, ValueError,
            ExecutionContractError, _ObservedProviderFailure):
        _fail(stage)
