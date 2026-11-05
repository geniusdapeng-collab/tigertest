"""Trusted research-frontier and acquisition contracts for the v2 workflow.

The frontier is a market-only research priority, never a trading approval.
Every byte used by scoring belongs to a frozen, independently replayable pack.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re

from . import config
from .agent_evidence import (EvidenceInvalid, atomic_json, json_bytes, normalize,
                            read_bytes, strict_json)
from .agents.universe_scanner_agent import rank_universe
from .chains import SECTOR_TO_CHAINS, chain_of
from .redline.execution import ExecutionContractError

ACQUISITION_CONTRACT = "tiger.intelligence-acquisition.v2"
SOURCE_CONTENT_CONTRACT = "intelligence-original-versions/v2"
SEARCH_MODE = "intelligence_source_registry"
FRONTIER_CONTRACT = "tiger.research-frontier.v2"
POLICY_VERSION = "tiger.intelligence-policy.2026-11-05"
# Tool admission is separate from the model's full legal context/output window.
# A denied or exhausted tool budget remains an explicit gap, never a fake fact.
TOOL_LIMITS = {"requests": 320, "fetches": 256, "bytes": 64 * 1024 * 1024,
               "rounds": 2, "concurrency": 4, "deadline_seconds": 600}
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}\Z")


def value_hash(value) -> str:
    return hashlib.sha256(json_bytes(normalize(value))).hexdigest()


def acquisition_policy() -> dict:
    return {"contract": ACQUISITION_CONTRACT, "policy_version": POLICY_VERSION,
            "source_content_contract": SOURCE_CONTENT_CONTRACT,
            "search_mode": SEARCH_MODE, "limits": dict(TOOL_LIMITS),
            "frontier": {"ranked": config.SCAN_TOP_N, "representatives_per_mapped_sector": 1,
                         "include_observed_holdings_and_pending": True,
                         "final_scan_outside_frontier": "disclose_and_refuse_entry"},
            "baseline": "explicit_snapshot_only_no_semantic_cache",
            "freeze": "revision-frontier-market-CAS",
            "unknown": "per_target_null_not_clean_risk"}


def _validate_input_packet(packet: dict) -> dict:
    """The public registry is recomputed, never trusted as a saved flag."""
    from .intelligence.registry import default_registry
    from .intelligence.contracts import safe_value

    if (not isinstance(packet, dict) or set(packet) != {"schema", "config", "registry", "baseline"}
            or packet["schema"] != "tiger.intelligence-input.v2"
            or not isinstance(packet["config"], dict)
            or safe_value(packet["config"]) != packet["config"]
            or default_registry(packet["config"]).public_manifest() != packet["registry"]):
        raise EvidenceInvalid("RESULT_INVALID", "Frozen intelligence source configuration differs")
    baseline = packet["baseline"]
    if baseline is not None and (not isinstance(baseline, dict)
            or not isinstance(baseline.get("manifest"), dict)
            or not isinstance(baseline.get("archive_root"), str)
            or not Path(baseline["archive_root"]).is_absolute()):
        raise EvidenceInvalid("RESULT_INVALID", "Unbound intelligence baseline")
    return packet


def validate_input_packet(packet: dict) -> dict:
    from .intelligence.contracts import IntelligenceError
    try:
        return _validate_input_packet(packet)
    except (IntelligenceError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceInvalid):
            raise
        raise EvidenceInvalid("RESULT_INVALID", "Frozen intelligence source configuration differs") from None


def _stage_input_packet(run: Path, *, as_of: datetime) -> dict:
    """Only trusted startup may supply a registry or explicit raw archive."""
    from .intelligence.registry import default_registry

    config = {}
    configured = os.environ.get("TIGER_INTEL_REGISTRY_FILE")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise EvidenceInvalid("INVALID_REQUEST", "Intelligence registry startup path must be absolute")
        config = strict_json(read_bytes(path.parent, path.name, limit=1024 * 1024))
    registry = default_registry(config)
    baseline = None
    archive = os.environ.get("TIGER_INTEL_ARCHIVE_ROOT")
    if archive:
        from .intelligence.service import IntelligenceService
        path = Path(archive)
        if not path.is_absolute() or not path.is_dir():
            raise EvidenceInvalid("INVALID_REQUEST", "Explicit intelligence archive must exist")
        service = IntelligenceService(path, registry, {}, readonly=True)
        manifest = service.archive_versions(as_of=as_of.isoformat())
        baseline = {"archive_root": str(path), "manifest": manifest}
    packet = {"schema": "tiger.intelligence-input.v2", "config": config,
              "registry": registry.public_manifest(), "baseline": baseline}
    validate_input_packet(packet)
    atomic_json(run, "intelligence-input.json", packet, exclusive=True)
    return packet


def stage_input_packet(run: Path, *, as_of: datetime) -> dict:
    from .intelligence.contracts import IntelligenceError
    try:
        return _stage_input_packet(run, as_of=as_of)
    except IntelligenceError:
        raise EvidenceInvalid("INVALID_REQUEST", "Trusted intelligence startup configuration is invalid") from None


def _held_symbols(portfolio_state: dict | None) -> tuple[list[str] | None, list[str]]:
    if portfolio_state is None:
        return None, []
    if not isinstance(portfolio_state, dict):
        raise ExecutionContractError("Invalid admitted portfolio for intelligence frontier")
    held, pending = [], []
    for name, target in (("positions", held), ("pending", pending)):
        rows = portfolio_state.get(name, [])
        if not isinstance(rows, list):
            raise ExecutionContractError("Invalid portfolio research records")
        for row in rows:
            ticker = row.get("ticker") if isinstance(row, dict) else None
            if not isinstance(ticker, str) or _SYMBOL.fullmatch(ticker) is None:
                raise ExecutionContractError("Invalid admitted portfolio security")
            if ticker not in target:
                target.append(ticker)
    return held, pending


def build_frontier(market_data: dict, universe: list[str], security_ids: dict[str, str], *,
                   as_of: datetime, market_snapshot_ref: dict,
                   portfolio_state: dict | None = None, scan_filters=None,
                   sector_symbols=None, top_n: int = config.SCAN_TOP_N) -> dict:
    """Reuse exact scanner rank/filter rules without SHS or entry supplements."""
    if (as_of.utcoffset() is None or type(top_n) is not int or not 1 <= top_n <= 1000
            or len(set(universe)) != len(universe)
            or any(symbol not in security_ids for symbol in universe)):
        raise ExecutionContractError("Unbound research frontier")
    ranked, rejected, audit = rank_universe(universe, market_data, scan_filters,
                                           capture_decisions=True)
    held, pending = _held_symbols(portfolio_state)
    selected, reasons = [], {}
    missing = []

    def include(symbol, reason):
        if symbol not in security_ids:
            missing.append({"symbol": symbol, "reason": "outside_reviewed_roster", "basis": reason})
            return
        if symbol not in market_data["stock_ohlcv"]:
            missing.append({"symbol": symbol, "reason": "market_observation_missing", "basis": reason})
            return
        if symbol not in selected:
            selected.append(symbol)
            reasons[symbol] = []
        if reason not in reasons[symbol]:
            reasons[symbol].append(reason)

    for symbol in [*(held or []), *pending]:
        include(symbol, "admitted_holding_or_pending")
    for row in ranked[:top_n]:
        include(row["ticker"], "market_rank")
    sector_gaps = []
    for sector in sector_symbols or []:
        chains = set(SECTOR_TO_CHAINS.get(sector, []))
        representative = next((row["ticker"] for row in ranked
                               if chain_of(row["ticker"])[0] in chains), None)
        if representative:
            include(representative, "reviewed_thematic_representative:" + sector)
        else:
            sector_gaps.append({"sector": sector, "status": "representative_unavailable",
                                "classification": "reviewed_thematic_mapping_only"})
    stock_hashes = {symbol: value_hash(market_data["stock_ohlcv"][symbol]) for symbol in selected}
    value = {"contract": FRONTIER_CONTRACT, "policy_version": POLICY_VERSION,
             "as_of": as_of.astimezone(timezone.utc).isoformat(),
             "universe": list(universe), "security_ids": dict(security_ids),
             "symbols": selected, "selection_reasons": reasons,
             "holdings": held, "pending": pending, "watchlist": [],
             "ranked": ranked, "rank_rejections": rejected, "rank_audit": audit,
             "missing": missing, "sector_gaps": sector_gaps,
             "market_snapshot_refs": {"stage_output": market_snapshot_ref,
                                      "benchmark_sha256": value_hash(market_data["spy"]),
                                      "stock_sha256": stock_hashes},
             "not_a_trade_approval": True}
    return {**value, "manifest_sha256": value_hash(value)}


def entity_registry(frontier: dict) -> dict:
    """Reviewed ticker identities, without invented company-name aliases."""
    symbols = frontier["security_ids"]
    return {"securities": dict(symbols), "entities": {
        identity: {"type": "security", "name": symbol, "aliases": [symbol],
                   "identifiers": {"ticker": [symbol]}, "security_ids": [identity]}
        for symbol, identity in symbols.items()}}


def verify_frontier(value: dict, market_data: dict, universe: list[str], *,
                    as_of: datetime, market_snapshot_ref: dict,
                    portfolio_state=None, scan_filters=None, sector_symbols=None) -> None:
    expected = build_frontier(market_data, universe, value["security_ids"], as_of=as_of,
        market_snapshot_ref=market_snapshot_ref, portfolio_state=portfolio_state,
        scan_filters=scan_filters, sector_symbols=sector_symbols)
    if normalize(value) != normalize(expected):
        raise ExecutionContractError("Research frontier differs from frozen market facts")


def qualify_candidates(context: dict, frontier: dict) -> list[dict]:
    """Keep final scanner supplements observable; unresearched ones cannot enter."""
    allowed = set(frontier["symbols"])
    rejected, retained = [], []
    for candidate in context.get("watchlist", []):
        if candidate.ticker in allowed:
            retained.append(candidate)
        else:
            rejected.append({"symbol": candidate.ticker,
                             "reason": "outside_frozen_research_frontier",
                             "next_action": "new_evidence_version_and_rescore"})
    context["watchlist"] = retained
    context["intelligence_exclusions"] = rejected
    context["scan_stats"] = {**context.get("scan_stats", {}),
                             "market_selected": context.get("scan_stats", {}).get("selected", len(retained) + len(rejected)),
                             "selected": len(retained),
                             "intelligence_selected": len(retained),
                             "intelligence_unresearched": len(rejected)}
    return rejected


def restore_frame(packet: dict):
    """Restore only explicit normalized OHLCV frames for deterministic replay."""
    import pandas as pd

    keys = {"__type__", "index", "index_names", "columns", "column_names", "dtypes", "values"}
    if (not isinstance(packet, dict) or set(packet) != keys or packet["__type__"] != "dataframe"
            or len(packet["dtypes"]) != len(packet["columns"])
            or any(not isinstance(column, str) for column in packet["columns"])):
        raise ExecutionContractError("Invalid normalized research market frame")
    index = []
    for label in packet["index"]:
        if not isinstance(label, dict) or label.get("__type__") != "Timestamp" or set(label) != {"__type__", "value"}:
            raise ExecutionContractError("Unverifiable research market index")
        index.append(pd.Timestamp(label["value"]))
    def number(item):
        if isinstance(item, dict) and item.get("__type__") == "missing":
            return float("nan")
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ExecutionContractError("Non-numeric research market observation")
        return item
    frame = pd.DataFrame([[number(item) for item in row] for row in packet["values"]],
                         index=pd.DatetimeIndex(index), columns=packet["columns"])
    frame.index.names = packet["index_names"]
    frame.columns.names = packet["column_names"]
    try:
        frame = frame.astype(dict(zip(packet["columns"], packet["dtypes"])))
    except (ValueError, TypeError) as exc:
        raise ExecutionContractError("Research market dtype differs") from exc
    if normalize(frame) != packet:
        raise ExecutionContractError("Research market frame failed lossless replay")
    return frame
