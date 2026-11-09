"""Immutable pending candidates and explicit subsequent research scopes.

Discovery never expands an already frozen research snapshot. A requested
supplement creates a new admission, observes market data again, and repeats
the evidence, semantic, numerical and account checks before any paper action.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

from .agent_evidence import read_json, normalize
from .tasks.contracts import value_hash

CANDIDATES = "tiger.pending-candidates/v1"
SCOPE = "tiger.research-scope/v1"
_REQUIRED = ["new_market_observation", "required_evidence", "all_semantic_analyses", "numeric_risk", "account_compare_and_swap"]


def _time(value):
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("SCOPE_TIMEZONE_REQUIRED")
    return instant


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("SCOPE_HASH_REQUIRED")
    return value


def _sealed(value):
    return {**value, "manifest_sha256": value_hash(value)}


def validate_candidates(value):
    fields = {"schema", "run_id", "tenant_id", "source_sha256", "policy_sha256", "input_sha256",
        "as_of", "deadline_at", "frontier_sha256", "candidates", "not_a_trade_approval", "manifest_sha256"}
    if (type(value) is not dict or set(value) != fields or value["schema"] != CANDIDATES
            or value["not_a_trade_approval"] is not True
            or value["manifest_sha256"] != value_hash({key: item for key, item in value.items() if key != "manifest_sha256"})
            or _time(value["deadline_at"]) <= _time(value["as_of"])):
        raise ValueError("CANDIDATE_MANIFEST_INVALID")
    for key in ("source_sha256", "policy_sha256", "input_sha256", "frontier_sha256"):
        _hash(value[key])
    if type(value["candidates"]) is not list:
        raise ValueError("CANDIDATE_MANIFEST_INVALID")
    ids, symbols = set(), set()
    for row in value["candidates"]:
        if (type(row) is not dict or set(row) != {"candidate_id", "symbol", "security_id", "status", "discovery",
                "market_snapshot_refs", "required_work", "approval_reused"}
                or row["symbol"] in symbols or row["candidate_id"] in ids
                or row["status"] != "pending_research" or row["approval_reused"] is not False
                or row["required_work"] != _REQUIRED
                or row["discovery"].get("selection") != "research_missing"
                or row["discovery"].get("market_selection") not in {"baseline", "supplement"}):
            raise ValueError("CANDIDATE_ROW_INVALID")
        symbols.add(row["symbol"]); ids.add(row["candidate_id"])
    return deepcopy(value)


def pending_candidates(frontier, scan_decisions, work_manifest):
    if set(scan_decisions) != set(frontier["universe"]):
        raise ValueError("CANDIDATE_SCANNER_COVERAGE_MISMATCH")
    rows = []
    for symbol in frontier["universe"]:
        decision = scan_decisions[symbol]
        if decision.get("selection") != "research_missing":
            continue
        if (symbol in frontier["symbols"] or decision.get("hard_filter") != "pass"
                or decision.get("market_selection") not in {"baseline", "supplement"}):
            raise ValueError("CANDIDATE_DISCOVERY_INVALID")
        identity = {"run_id": work_manifest["run_id"], "frontier_sha256": frontier["manifest_sha256"],
                    "symbol": symbol, "security_id": frontier["security_ids"][symbol], "discovery": decision}
        rows.append({"candidate_id": "candidate_" + value_hash(identity)[:32],
            "symbol": symbol, "security_id": frontier["security_ids"][symbol], "status": "pending_research",
            "discovery": deepcopy(decision), "market_snapshot_refs": deepcopy(frontier["market_snapshot_refs"]),
            "required_work": list(_REQUIRED), "approval_reused": False})
    value = {"schema": CANDIDATES, **{key: work_manifest[key] for key in
        ("run_id", "tenant_id", "source_sha256", "policy_sha256", "input_sha256", "as_of", "deadline_at")},
        "frontier_sha256": frontier["manifest_sha256"], "candidates": rows, "not_a_trade_approval": True}
    return validate_candidates(_sealed(value))


def validate_scope(scope):
    fields = {"schema", "mode", "parent_run_id", "paper_account", "parent_receipt_sha256",
        "parent_candidate_manifest", "parent_candidate_manifest_sha256", "symbols", "security_ids", "requested_at",
        "original_signal_expired", "reuse_trade_approval", "required_work", "manifest_sha256"}
    if (type(scope) is not dict or set(scope) != fields or scope["schema"] != SCOPE
            or scope["mode"] != "new_research_admission" or scope["reuse_trade_approval"] is not False
            or scope["required_work"] != _REQUIRED
            or scope["manifest_sha256"] != value_hash({key: value for key, value in scope.items() if key != "manifest_sha256"})):
        raise ValueError("RESEARCH_SCOPE_INVALID")
    parent = validate_candidates(scope["parent_candidate_manifest"])
    requested = _time(scope["requested_at"])
    available = {row["symbol"]: row for row in parent["candidates"]}
    symbols = scope["symbols"]
    if (type(symbols) is not list or not symbols or len(symbols) != len(set(symbols))
            or not set(symbols) <= available.keys() or scope["parent_run_id"] != parent["run_id"]
            or scope["paper_account"] != parent["tenant_id"]
            or scope["parent_candidate_manifest_sha256"] != parent["manifest_sha256"]
            or scope["security_ids"] != {symbol: available[symbol]["security_id"] for symbol in symbols}
            or requested < _time(parent["as_of"])
            or type(scope["original_signal_expired"]) is not bool
            or scope["original_signal_expired"] != (requested >= _time(parent["deadline_at"]))):
        raise ValueError("RESEARCH_SCOPE_IDENTITY_MISMATCH")
    _hash(scope["parent_receipt_sha256"])
    return deepcopy(scope)


def create_scope(pending, symbols, *, parent_receipt_sha256, requested_at):
    parent = validate_candidates(pending)
    if type(symbols) is not list or not symbols or any(type(symbol) is not str for symbol in symbols):
        raise ValueError("RESEARCH_SCOPE_SYMBOLS_REQUIRED")
    available = {row["symbol"]: row for row in parent["candidates"]}
    if len(symbols) != len(set(symbols)) or not set(symbols) <= available.keys():
        raise ValueError("RESEARCH_SCOPE_OUTSIDE_PENDING")
    requested = _time(requested_at)
    value = {"schema": SCOPE, "mode": "new_research_admission", "parent_run_id": parent["run_id"],
        "paper_account": parent["tenant_id"], "parent_receipt_sha256": _hash(parent_receipt_sha256),
        "parent_candidate_manifest": parent, "parent_candidate_manifest_sha256": parent["manifest_sha256"],
        "symbols": list(symbols), "security_ids": {symbol: available[symbol]["security_id"] for symbol in symbols},
        "requested_at": requested.isoformat(), "original_signal_expired": requested >= _time(parent["deadline_at"]),
        "reuse_trade_approval": False, "required_work": list(_REQUIRED)}
    return validate_scope(_sealed(value))


def apply_scope(frontier, scope, observed_stocks):
    scope = validate_scope(scope)
    if _time(frontier["as_of"]) < _time(scope["requested_at"]):
        raise ValueError("SCOPE_REQUIRES_NEW_ADMISSION")
    value = deepcopy(frontier)
    value.pop("manifest_sha256", None)
    if any(symbol not in frontier["universe"] or frontier["security_ids"].get(symbol) != identity
           for symbol, identity in scope["security_ids"].items()):
        raise ValueError("SCOPE_SECURITY_IDENTITY_CHANGED")
    for symbol in scope["symbols"]:
        if symbol not in observed_stocks:
            value["missing"].append({"symbol": symbol, "reason": "market_observation_missing",
                                     "basis": "authorized_pending_research"})
            continue
        if symbol not in value["symbols"]:
            value["symbols"].append(symbol)
            value["selection_reasons"][symbol] = []
        if "authorized_pending_research" not in value["selection_reasons"][symbol]:
            value["selection_reasons"][symbol].append("authorized_pending_research")
        value["market_snapshot_refs"]["stock_sha256"][symbol] = value_hash(normalize(observed_stocks[symbol]))
    value["supplement_scope_sha256"] = scope["manifest_sha256"]
    return _sealed(value)


def _outcome_rows(pending, *, observed_at, successor=None):
    """Pure display projection; only candidate_outcomes loads a successor."""
    expiry = _time(observed_at) >= _time(pending["deadline_at"])
    result = []
    for candidate in pending["candidates"]:
        symbol = candidate["symbol"]
        status = "expired_pending_research" if expiry else "pending_research"
        if successor is not None and symbol in successor["researched_symbols"]:
            status = ("selected_after_research" if symbol in successor["selected_symbols"] else
                      "rejected_after_research" if successor["research_sufficient"] else "pending_evidence")
        result.append({"candidate_id": candidate["candidate_id"], "symbol": symbol, "status": status,
            "successor_run_id": successor["run_id"] if successor is not None else None,
            "original_decision_unchanged": True})
    return {"schema": "tiger.candidate-outcomes/v1", "parent_run_id": pending["run_id"],
        "observed_at": _time(observed_at).isoformat(), "rows": result}


def candidate_outcomes(pending, successor_run, *, observed_at):
    pending = validate_candidates(pending)
    successor = None
    if successor_run is not None:
        from .agent_runtime import verify_run
        if not isinstance(successor_run, Path) or not successor_run.is_absolute():
            raise ValueError("VERIFIED_SUCCESSOR_PATH_REQUIRED")
        verified = verify_run(successor_run.parent.parent, successor_run.name, current_source=None)
        scope = validate_scope(read_json(successor_run, "research-scope.json"))
        if (scope["parent_candidate_manifest"] != pending
                or scope["parent_candidate_manifest_sha256"] != pending["manifest_sha256"]):
            raise ValueError("SUCCESSOR_SCOPE_MISMATCH")
        summary = verified["summary"]
        intelligence = summary["disclosures"]["intelligence"]
        successor = {"run_id": successor_run.name,
            "selected_symbols": [row["ticker"] for row in summary["picks"]],
            "researched_symbols": intelligence["frontier"]["symbols"],
            "research_sufficient": intelligence["bundle"]["state"] == "completed"
                and not intelligence["bundle"]["missing_required"]}
    return _outcome_rows(pending, observed_at=observed_at, successor=successor)
