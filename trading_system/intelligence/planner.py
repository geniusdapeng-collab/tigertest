"""Decision-question coverage and provider-specific bounded query compilation."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import re

from .contracts import PLAN_SCHEMA, IntelligenceError, SearchRequest, aware, digest, identifier, safe_value


DEFAULT_LIMITS = {"requests": 240, "fetches": 140, "bytes": 64 * 1024 * 1024,
                  "rounds": 3, "concurrency": 4, "deadline_seconds": 1800}
_CHAINS = ("memory", "logic", "foundry", "equipment", "ai_model", "ai_app")
_SECTORS = ("XLK", "SMH", "XLE", "XLF", "XLV", "IBB", "XLY", "XLP", "XLI", "XLU", "XLRE", "IWM")


def _cell(target, question, query, *, material="news", required=True, entity=None, topic=None, window_days=7):
    value = {"target_id": target, "entity_id": entity, "topic": topic, "question": question,
             "query": query, "required": required, "material_type": material, "window_days": window_days}
    return {"cell_id": "cell_" + digest(value)[:24], **value}


def build_plan(request: dict, registry) -> dict:
    if not isinstance(request, dict):
        raise IntelligenceError("CONTRACT_INVALID", "Plan request must be an object")
    if safe_value(request) != request:
        raise IntelligenceError("POLICY_DENIED", "Credentials or unsafe source URLs cannot be persisted in a plan")
    acquisition_mode = request.get("acquisition_mode", "live")
    if not isinstance(acquisition_mode, str) or acquisition_mode not in {"live", "snapshot_only"}:
        raise IntelligenceError("CONTRACT_INVALID", "Unknown acquisition mode")
    run_id = identifier(request.get("run_id"), "run id")
    cutoff = aware(request.get("as_of"))
    if request.get("market", "us") != "us":
        raise IntelligenceError("POLICY_DENIED", "This acquisition profile is bound to US research")
    universe = request.get("universe", {})
    if not isinstance(universe, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in universe.items()):
        raise IntelligenceError("CONTRACT_INVALID", "Universe must map tickers to frozen security ids")
    for key, value in universe.items():
        identifier(key, "ticker")
        identifier(value, "security id")
    frontier, watchlist, holdings = request.get("frontier", []), request.get("watchlist", []), request.get("holdings")
    pending = request.get("pending", [])
    if not all(isinstance(x, list) for x in (frontier, watchlist, pending)) or holdings is not None and not isinstance(holdings, list):
        raise IntelligenceError("CONTRACT_INVALID", "Frontier, watchlist, pending and connected holdings must be arrays")
    for symbol in [*frontier, *(holdings or []), *watchlist, *pending]:
        identifier(symbol, "frontier/holding/watchlist symbol")
    if any(symbol not in universe for symbol in [*frontier, *watchlist]):
        raise IntelligenceError("POLICY_DENIED", "Research frontier contains a security outside the frozen universe")
    unavailable_holdings = list(dict.fromkeys(symbol for symbol in (holdings or []) if symbol not in universe))
    unavailable_pending = list(dict.fromkeys(symbol for symbol in pending if symbol not in universe))
    selected = list(dict.fromkeys([*frontier, *(symbol for symbol in (holdings or []) if symbol in universe),
                                   *watchlist, *(symbol for symbol in pending if symbol in universe)]))
    frontier_hash = request.get("frontier_manifest_hash")
    if not isinstance(frontier_hash, str) or re.fullmatch(r"[0-9a-f]{64}", frontier_hash) is None:
        raise IntelligenceError("PLAN_CONFLICT", "A frozen frontier hash is required")
    entities = request.get("entities") or {}
    if isinstance(entities, list):
        if any(not isinstance(x, dict) or not x.get("id") for x in entities):
            raise IntelligenceError("CONTRACT_INVALID", "Entity array entries need identities")
        entities = {item["id"]: {k: v for k, v in item.items() if k != "id"} for item in entities}
    if not isinstance(entities, dict):
        raise IntelligenceError("CONTRACT_INVALID", "Entities must be registered objects")
    entities = {identifier(key, "entity id"): dict(value) for key, value in entities.items()}
    for entity in entities.values():
        ids = entity.get("identifiers", [])
        if isinstance(ids, dict):
            flattened = []
            for scheme, values in ids.items():
                values = [values] if isinstance(values, str) else values
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise IntelligenceError("CONTRACT_INVALID", "Entity identifiers must contain string values")
                flattened.extend({"scheme": scheme, "value": value} for value in values)
            entity["identifiers"] = flattened
        elif not isinstance(ids, list) or any(not isinstance(item, dict) or not isinstance(item.get("scheme"), str)
                or not isinstance(item.get("value"), str) for item in ids):
            raise IntelligenceError("CONTRACT_INVALID", "Entity identifiers must be a mapping or typed array")
    for symbol in selected:
        entities.setdefault(universe[symbol], {"type": "security", "name": symbol, "tickers": [symbol],
                                             "aliases": [], "identifiers": []})
    requested_sources = request.get("source_ids", registry.source_ids())
    if not isinstance(requested_sources, list) or len(set(requested_sources)) != len(requested_sources):
        raise IntelligenceError("CONTRACT_INVALID", "Source ids must be a unique array")
    for source_id in requested_sources:
        registry.get(source_id)
    limits = {**DEFAULT_LIMITS, **request.get("limits", {})}
    if set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()) or limits["concurrency"] > 4:
        raise IntelligenceError("CONTRACT_INVALID", "Acquisition limits must be positive, with concurrency at most four")
    topics = request.get("topics", {})
    if not isinstance(topics, dict) or any(not isinstance(v, str) or not v.strip() for v in topics.values()):
        raise IntelligenceError("CONTRACT_INVALID", "Topics must map to explicit research questions")
    cells = request.get("required_cells")
    if cells is not None:
        if not isinstance(cells, list) or not cells:
            raise IntelligenceError("CONTRACT_INVALID", "Explicit coverage must be a nonempty array")
        cells = [dict(c) for c in cells]
        for cell in cells:
            if not all(k in cell for k in ("target_id", "question", "required")) or type(cell["required"]) is not bool:
                raise IntelligenceError("CONTRACT_INVALID", "Coverage cells need target, question and required status")
            cell.setdefault("query", cell["question"])
            cell.setdefault("entity_id", None)
            cell.setdefault("topic", None)
            cell.setdefault("material_type", "news")
            cell.setdefault("window_days", 7)
            cell.setdefault("cell_id", "cell_" + digest(cell)[:24])
    else:
        cells = [_cell("global:macro", "Latest announced policy and statistical releases",
            "Federal Reserve FOMC CPI employment inflation policy", material="macro_release")]
        for topic_id, query in topics.items():
            target = "chain:" + topic_id if topic_id in _CHAINS else "sector:" + topic_id.upper() if topic_id.upper() in _SECTORS else "topic:" + topic_id
            cells.append(_cell(target, query, query, topic=topic_id))
        if not topics:
            for chain in _CHAINS:
                cells.append(_cell("chain:" + chain, chain + " supply demand risks", chain + " supply demand capacity risks", topic=chain))
            for sector in _SECTORS:
                cells.append(_cell("sector:" + sector, sector + " sector catalysts and risks", sector + " earnings guidance adverse events", topic=sector))
        for symbol in selected:
            entity = entities[universe[symbol]]
            name = entity.get("name") or symbol
            target = "security:" + universe[symbol]
            for question, suffix, material, days in (
                ("Latest financial baseline and guidance", "earnings annual quarterly report guidance", "issuer_financial", 760),
                ("Current events and material adverse developments", "recall lawsuit investigation warning material event", "adverse_event", 30),
                ("Counterevidence, corrections and withdrawals", "correction restatement withdrawal contrary evidence risks", "counterevidence", 30)):
                cells.append(_cell(target, question, name + " " + symbol + " " + suffix,
                    material=material, entity=universe[symbol], window_days=days))
        if holdings is None:
            cells.append(_cell("account:holdings", "Holdings connection state", "Holdings are not connected",
                               material="connection", required=False))
    for symbol in unavailable_holdings:
        cells.append({**_cell("account:holding:" + symbol, "Connected holding is outside the reviewed roster",
            "Holding " + symbol + " has no reviewed security identity", material="connection", required=True),
            "holding_symbol": symbol, "connection_reason": "HOLDING_OUTSIDE_REVIEWED_ROSTER"})
    for symbol in unavailable_pending:
        cells.append({**_cell("account:pending:" + symbol, "Pending order is outside the reviewed roster",
            "Pending order " + symbol + " has no reviewed security identity", material="connection", required=True),
            "pending_symbol": symbol, "connection_reason": "PENDING_OUTSIDE_REVIEWED_ROSTER"})
    if len({c["cell_id"] for c in cells}) != len(cells):
        raise IntelligenceError("CONTRACT_INVALID", "Coverage cell ids must be unique")
    for cell in cells:
        identifier(cell["cell_id"])
        identifier(cell["target_id"])
        if (not isinstance(cell["query"], str) or not cell["query"].strip()
                or type(cell["window_days"]) is not int or cell["window_days"] <= 0):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid coverage query or window")
    search_sources = [s for s in requested_sources if registry.get(s).kind == "search"]
    queries = []
    if search_sources:
        for cell in cells:
            if cell["material_type"] == "connection":
                continue
            queries.append({"source_id": search_sources[0], "cell_ids": [cell["cell_id"]],
                "request": SearchRequest(cell["query"], count=10, language="en",
                    start_date=(cutoff.date() - timedelta(days=cell["window_days"])).isoformat(),
                    end_date=cutoff.date().isoformat(), topic="news",
                    entities=(cell["entity_id"],) if cell["entity_id"] else (), targets=(cell["target_id"],),
                    context={"material_type": cell["material_type"]}).to_dict()})
    for source_id in requested_sources:
        spec = registry.get(source_id)
        if spec.kind in {"search", "web_fetch"}:
            continue
        if spec.kind == "sec_submissions":
            for symbol in selected:
                entity_id = universe[symbol]
                entity = entities[entity_id]
                cik = next((x.get("value") for x in entity.get("identifiers", []) if x.get("scheme", "").lower() == "cik"), None)
                target = "security:" + entity_id
                queries.insert(0, {"source_id": source_id, "cell_ids": [c["cell_id"] for c in cells if c["target_id"] == target and c["material_type"] == "issuer_financial"],
                    "request": SearchRequest(symbol + " official disclosures", count=50,
                        start_date=(cutoff.date() - timedelta(days=760)).isoformat(), end_date=cutoff.date().isoformat(),
                        entities=(entity_id,), targets=(target,), context={"cik": cik, "ticker": symbol,
                            "issuer_id": entity_id, "material_type": "issuer_financial"}).to_dict()})
        else:
            targets = tuple(c["target_id"] for c in cells if c["material_type"] != "connection")
            queries.insert(0, {"source_id": source_id, "cell_ids": [c["cell_id"] for c in cells if c["material_type"] != "connection"],
                "request": SearchRequest("registered official source baseline", count=50,
                    start_date=(cutoff.date() - timedelta(days=450)).isoformat(), end_date=cutoff.date().isoformat(),
                    entities=tuple(universe[x] for x in selected), targets=targets,
                    context={"material_type": spec.kind, "vintage_date": cutoff.date().isoformat()}).to_dict()})
    priority = {"sec_directory": 0, "sec_submissions": 1, "feed": 2, "issuer_rss": 2, "page": 2,
                "bls_calendar": 3, "fred": 3, "cboe_csv": 3, "search": 4}
    queries.sort(key=lambda q: priority[registry.get(q["source_id"]).kind])
    result = {"schema": PLAN_SCHEMA, "run_id": run_id, "market": "us", "as_of": cutoff.isoformat(),
        "as_of_basis": request.get("as_of_basis", "strict_observed_version"), "universe": universe,
        "entity_registry": entities, "frontier": selected, "frontier_manifest_hash": frontier_hash,
        "allowed_entities": sorted({universe[x] for x in selected}),
        "market_snapshot_refs": request.get("market_snapshot_refs", []), "holdings": holdings,
        "unavailable_holdings": unavailable_holdings,
        "pending": pending, "unavailable_pending": unavailable_pending,
        "watchlist": watchlist, "coverage_cells": cells, "query_plan": queries, "source_ids": requested_sources,
        "source_registry_sha256": registry.public_manifest()["sha256"], "limits": limits,
        "baseline_refs": request.get("baseline_refs", []), "execution": {"effective_concurrency": 1,
            "reason": "Bounded serial dispatch preserves main-thread execution evidence writes"}}
    if result["as_of_basis"] not in {"strict_observed_version", "historical_reconstruction"}:
        raise IntelligenceError("CONTRACT_INVALID", "Unknown historical evidence basis")
    if acquisition_mode == "snapshot_only":
        # Keep the complete decision scope and source policy. This is a trusted
        # no-dispatch choice, never permission to shrink required coverage.
        result["acquisition_mode"] = acquisition_mode
        result["query_plan"] = []
    return {**result, "plan_hash": digest(result)}


def compile_requests(source_id: str, request: SearchRequest) -> list[SearchRequest]:
    """Split a long question without dropping any date, language or site condition."""
    domains = [(domain,) for domain in request.domains] if len(request.domains) > 1 else [request.domains]
    result = []
    for domain in domains:
        part = replace(request, domains=domain)
        if source_id == "deepseek":
            from .adapters import deepseek_query
            overhead = len(deepseek_query(replace(part, query="x"))) - 1
            allowance = 128 - overhead
            words, groups, current = part.query.split(), [], ""
            for word in words:
                if len(word) > allowance:
                    raise IntelligenceError("CONTRACT_INVALID", "One query term exceeds provider request contract")
                candidate = (current + " " + word).strip()
                if len(candidate) > allowance:
                    groups.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                groups.append(current)
            for index, group in enumerate(groups):
                result.append(replace(part, query=group, context={**part.context,
                    "original_query": request.query, "query_piece": index + 1, "query_pieces": len(groups)}))
        elif source_id == "brave" and (len(part.query) > 350 or len(part.query.split()) > 45):
            words, groups, current = part.query.split(), [], []
            for word in words:
                if len(word) > 300:
                    raise IntelligenceError("CONTRACT_INVALID", "One query term exceeds provider contract")
                if len(" ".join([*current, word])) > 300 or len(current) >= 40:
                    groups.append(" ".join(current)); current = []
                current.append(word)
            if current:
                groups.append(" ".join(current))
            result.extend(replace(part, query=q, context={**part.context, "original_query": request.query,
                "query_piece": i + 1, "query_pieces": len(groups)}) for i, q in enumerate(groups))
        else:
            result.append(part)
    return result
