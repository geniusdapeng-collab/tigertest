"""Trusted research-frontier and acquisition contracts for the v2 workflow.

The frontier is a market-only research priority, never a trading approval.
Every byte used by scoring belongs to a frozen, independently replayable pack.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
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
PAPER_SNAPSHOT_LIMITS = {"documents": 256, "scan_candidates": 2048,
    "bytes": 64 * 1024 * 1024, "metadata_bytes": 8 * 1024 * 1024,
    "deadline_seconds": 30, "associations": 8192}
SNAPSHOT_ONLY_METADATA_BYTES = 32 * 1024 * 1024
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


def baseline_manifest_hash(manifest):
    from .intelligence.contracts import digest
    excluded = {"manifest_sha256", "snapshot_id"} if manifest.get("schema") == "topic_snapshot/v1" else {"manifest_sha256"}
    return digest({key: value for key, value in manifest.items() if key not in excluded})


def _validate_baseline_manifest(manifest):
    """Validate a frozen envelope without reading or re-exporting a live archive."""
    from .intelligence.snapshots import _normalize
    from .intelligence.contracts import aware, timestamp
    if (not isinstance(manifest, dict) or manifest.get("schema") not in {"tiger.intelligence-baseline/v1", "topic_snapshot/v1"}
            or manifest.get("tenant") != "public" or not isinstance(manifest.get("versions"), list)
            or manifest.get("manifest_sha256") != baseline_manifest_hash(manifest)):
        raise EvidenceInvalid("RESULT_INVALID", "Frozen baseline manifest differs")
    if manifest["schema"] == "topic_snapshot/v1":
        normalized_scope, normalized_limits = _normalize(manifest["scope"], manifest["limits"])
        if (manifest["snapshot_id"] != "snapshot_" + manifest["manifest_sha256"][:32]
                or manifest["archive_id"] != manifest["origin_archive_id"]
                or manifest["as_of"] != manifest["cutoff"]
                or manifest["state"] not in {"partial", "completed"}
                or manifest["state"] != ("partial" if manifest["gaps"] else "completed")
                or normalized_scope != manifest["scope"] or normalized_limits != manifest["limits"]
                or len(manifest["versions"]) != manifest["counts"]["selected"]
                or len(manifest["versions"]) != len(manifest["members"])
                or len(manifest["versions"]) > manifest["limits"]["documents"]
                or len({member["id"] for member in manifest["members"]}) != len(manifest["members"])
                or manifest["event_head"] != manifest["fixed_event_frontier"]
                or timestamp(manifest["cutoff"]) != manifest["cutoff"]):
            raise EvidenceInvalid("RESULT_INVALID", "Scoped baseline snapshot differs")
        head = manifest["fixed_event_frontier"]
        if (set(head) != {"sequence", "sha256"} or type(head["sequence"]) is not int or head["sequence"] < 0
                or (head["sha256"] is not None if head["sequence"] == 0 else
                    not isinstance(head["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", head["sha256"]) is None)):
            raise EvidenceInvalid("RESULT_INVALID", "Scoped baseline frontier differs")
        selected_bytes, association_count = 0, 0
        for member, version in zip(manifest["members"], manifest["versions"], strict=True):
            expected = {"id": member["id"], "doc_id": member["doc_id"], "version": member["version"],
                "text_sha256": member["text_blob"]["sha256"], "raw_sha256": member["raw_blob"]["sha256"],
                "first_seen_at": member["first_seen_at"], "source_id": member["source_id"],
                "entity_ids": member["requested_entity_ids"], "document_sha256": member["document_sha256"]}
            if (version != expected or member["entity_ids"] != member["requested_entity_ids"]
                    or aware(member["first_seen_at"]) > aware(manifest["cutoff"])
                    or manifest["scope"]["observed_since"] and aware(member["first_seen_at"]) < aware(manifest["scope"]["observed_since"])):
                raise EvidenceInvalid("RESULT_INVALID", "Scoped member version differs")
            for field in ("requested_entity_ids", "topic_ids"):
                values = member[field]
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or values != sorted(set(values)):
                    raise EvidenceInvalid("RESULT_INVALID", "Scoped member labels differ")
            for blob in (member["raw_blob"], member["text_blob"]):
                if (type(blob["bytes"]) is not int or blob["bytes"] < 0
                        or not isinstance(blob["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", blob["sha256"]) is None):
                    raise EvidenceInvalid("RESULT_INVALID", "Scoped member blob differs")
                selected_bytes += blob["bytes"]
            association_count += len(member["membership_provenance"])
        if (selected_bytes > manifest["limits"]["bytes"]
                or selected_bytes != manifest["counts"]["selected_raw_text_bytes"]
                or association_count > manifest["limits"]["associations"]):
            raise EvidenceInvalid("RESULT_INVALID", "Scoped baseline limits differ")
    return manifest


def validate_baseline_manifest(manifest):
    from .intelligence.contracts import IntelligenceError
    try:
        return _validate_baseline_manifest(manifest)
    except EvidenceInvalid:
        raise
    except (IntelligenceError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise EvidenceInvalid("RESULT_INVALID", "Frozen baseline manifest differs") from None


def baseline_version_ids(manifest, *, entity_ids=None, target_ids=None):
    """Derive a frontier subset from already sealed members, never late evidence.

    Query labels are only selection provenance; all semantic subject/action
    qualification still happens on the original text in this new run.
    """
    validate_baseline_manifest(manifest)
    if manifest["schema"] != "topic_snapshot/v1" or entity_ids is None:
        return [row["id"] for row in manifest["versions"]]
    entities = set(entity_ids)
    targets = set(target_ids or ())
    targets.update(value.split(":", 1)[-1] for value in tuple(targets) if value.startswith(("chain:", "sector:", "topic:")))
    selected = set()
    for member in manifest["members"]:
        subjects = {row if isinstance(row, str) else row.get("entity_id", row.get("id"))
                    for row in member.get("validated_subjects", []) if isinstance(row, (str, dict))}
        if entities.intersection(member["requested_entity_ids"]) or entities.intersection(subjects) or targets.intersection(member["topic_ids"]):
            selected.add(member["id"])
    return [row["id"] for row in manifest["versions"] if row["id"] in selected]


def _validate_input_packet(packet: dict) -> dict:
    """The public registry is recomputed, never trusted as a saved flag."""
    from .intelligence.registry import acquisition_mode, default_registry
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
    if baseline is not None:
        validate_baseline_manifest(baseline["manifest"])
    if acquisition_mode(packet["config"]) == "snapshot_only" and (
            baseline is None or not baseline["manifest"]["versions"]):
        raise EvidenceInvalid("RESULT_INVALID", "Snapshot-only acquisition requires a nonempty explicit raw baseline")
    if (acquisition_mode(packet["config"]) == "snapshot_only"
            and (baseline["manifest"]["schema"] != "topic_snapshot/v1"
                or baseline["manifest"]["limits"] != {**PAPER_SNAPSHOT_LIMITS, "metadata_bytes": SNAPSHOT_ONLY_METADATA_BYTES})):
        raise EvidenceInvalid("RESULT_INVALID", "Snapshot-only baseline limits differ from its trusted admission policy")
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
    from .intelligence.registry import acquisition_mode, default_registry

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
    if acquisition_mode(config) == "snapshot_only" and not archive:
        raise EvidenceInvalid("INVALID_REQUEST", "Snapshot-only acquisition requires an explicit raw archive")
    if archive:
        from .intelligence.archive import Archive
        from .universe import EXTENDED_UNIVERSE
        from .tech_chain.universe import TECH_SUBCHAINS
        from .markets import get_market
        path = Path(archive)
        if not path.is_absolute() or not path.is_dir():
            raise EvidenceInvalid("INVALID_REQUEST", "Explicit intelligence archive must exist")
        # The numerical frontier does not exist until market.prepare in the
        # worker. Seal a bounded trusted-roster superset first, then derive the
        # exact frontier subset from these immutable members after that stage.
        scope = {"entity_ids": ["us:" + symbol for symbol in EXTENDED_UNIVERSE],
            "topic_ids": ["global:macro", *TECH_SUBCHAINS, *get_market("us").sector_symbols],
            "observed_since": (as_of - timedelta(days=760)).isoformat(), "include_history": True}
        limits = dict(PAPER_SNAPSHOT_LIMITS)
        if acquisition_mode(config) == "snapshot_only":
            limits["metadata_bytes"] = SNAPSHOT_ONLY_METADATA_BYTES
        manifest = Archive(path).create_snapshot(as_of.isoformat(), scope=scope,
            limits=limits, registry_hash=registry.public_manifest()["sha256"],
            topic_ref={"purpose": "paper-admission-reviewed-roster-superset"})
        if acquisition_mode(config) == "snapshot_only" and not manifest["versions"]:
            raise EvidenceInvalid("INVALID_REQUEST", "Snapshot-only acquisition has no admitted raw originals")
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
