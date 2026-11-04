"""Reconstruct the strict research funnel and health from frozen stage evidence."""
from collections import Counter
from pathlib import Path

from .agent_evidence import EvidenceInvalid, read_bytes, read_snapshot, strict_json

SOURCE_PATH = "trading_system/research_audit.py"
CONTRACT = "tiger.research-audit.v1"
_GATES = ("market_data", "hard_filter", "entry_setup", "numeric_risk", "portfolio", "company", "model")
_REQUIRED = {"pipeline.prepare", "layer0.scan", "layer3.tss", "layer4.risk",
             "decision.debate", "review.daily"}
_PORTFOLIO_LIMITS = {"position_slots_exhausted", "group_risk_budget_exhausted", "gross_budget_exhausted"}
_MISSING_RISK = {"sector_evidence_missing", "sector_chase_evidence_missing", "existing_budget_unverifiable"}


def _gate(status, *reasons):
    return {"status": status, "reasons": list(reasons)}


def candidate_funnel(universe, scan, scored, risk, debate, roster=None):
    """One auditable row per scanned ticker; each gate partitions its denominator."""
    symbols = (roster or {}).get("symbols", {})
    if len(universe) != len(set(universe)) or set(universe) != set(scan):
        raise ValueError("Scanner coverage differs from the frozen stock universe")
    if roster and set(symbols) != set(universe):
        raise ValueError("Security roster differs from the frozen stock universe")
    scores = {row["fields"]["ticker"]: row["fields"] for row in scored}
    risks = {row["ticker"]: row for row in risk["candidates"]}
    expected = {ticker for ticker, row in scan.items()
                if row.get("selection") in {"baseline", "supplement"}}
    if (len(scores) != len(scored) or len(risks) != len(risk["candidates"])
            or expected != scores.keys() or scores.keys() != risks.keys()):
        raise ValueError("Deep scoring and risk rows differ from the scanner shortlist")
    companies = debate["company_evidence"]["candidates"]
    approved = {ticker for ticker, row in risks.items() if row["status"] == "accepted"}
    model = {row["symbol"]: row["action"] for row in debate["execution_decisions"]}
    if set(companies) != approved or not approved <= model.keys():
        raise ValueError("Final company and model evidence differs from approved risk candidates")
    rows = []
    for ticker in universe:
        scan_row = scan[ticker]
        filtered = scan_row["hard_filter"]
        selection = scan_row.get("selection")
        candidate = scores.get(ticker)
        risk_row = risks.get(ticker)
        company = companies.get(ticker)
        action = model.get(ticker)
        template = candidate.get("entry_template") if candidate else None
        no_data = filtered in {"no_data", "short_history"}
        gates = dict.fromkeys(_GATES, _gate("not_applicable"))
        gates["market_data"] = _gate("unknown", filtered) if no_data else _gate("passed")
        gates["hard_filter"] = (_gate("unknown", filtered) if no_data else
                                _gate("passed") if filtered == "pass" else _gate("rejected", filtered))
        if filtered == "pass":
            if template in {"A", "B", "C"}:
                gates["entry_setup"] = _gate("passed")
            elif selection == "shape_missing":
                gates["entry_setup"] = _gate("unknown", selection)
            else:
                gates["entry_setup"] = _gate("rejected", "no_entry_setup")
        if risk_row:
            status = risk_row["status"]
            reasons = risk_row["reasons"]
            account_limit = bool(set(reasons) & _PORTFOLIO_LIMITS)
            if status == "accepted" or account_limit:
                gates["numeric_risk"] = _gate("passed")
                gates["portfolio"] = (_gate("passed") if status == "accepted"
                                      else _gate("rejected", *reasons))
            else:
                gates["numeric_risk"] = _gate("unknown" if set(reasons) & _MISSING_RISK
                                               else "rejected", *reasons)
        if company:
            verified = company["status"] == "filing_verified"
            gates["company"] = _gate("passed") if verified else _gate("unknown", company["status"])
            gates["model"] = (_gate("passed") if action == "buy" else
                              _gate("rejected", f"model_{action}") if verified else
                              _gate("unknown", company["status"]))
        rows.append({
            "security_id": symbols.get(ticker, f"us:{ticker}"), "ticker": ticker,
            "selection": selection, "entry_template": template or None,
            "options_evidence": ("not_applicable" if candidate is None else
                                 "available" if candidate.get("s_options") is not None else "missing"),
            "model_action": action, "company_source_ids": company["source_ids"] if company else [],
            "gates": gates,
        })
    gates = {}
    for name in _GATES:
        counts = Counter(row["gates"][name]["status"] for row in rows)
        reasons = Counter(reason for row in rows for reason in row["gates"][name]["reasons"])
        gates[name] = {"denominator": len(rows) - counts["not_applicable"],
                       **{status: counts[status] for status in ("passed", "rejected", "unknown", "not_applicable")},
                       "overlapping_reasons": dict(sorted(reasons.items()))}
    return {"universe_size": len(rows), "gates": gates, "rows": rows,
            "roster_basis": (roster or {}).get("basis", "run_scoped_ticker"),
            "roster_sha256": (roster or {}).get("sha256")}


def _stage_record(event, output, bound_input=None):
    calls = output["io_calls"]
    failed = sum("error_ref" in call for call in calls)
    value = output["value"]
    gaps = []
    if event["status"] != "completed":
        gaps.append(event["status"])
    if event["stage"] == "tech.cycle_linkage" and not value:
        gaps.append("empty_linkage")
    if event["stage"] == "clean.cross_validate" and value["stats"].get("downgraded"):
        gaps.append("event_evidence_downgraded")
    if (event["stage"] in {"tech.risk", "tech.fusion"}
            and (bound_input or {}).get("risk_coverage", {}).get("status") == "insufficient_evidence"):
        gaps.append("scorable_risk_evidence_missing")
    if event["stage"] == "data.prepare":
        freshness = value["market_data"]["freshness"].get("market_close", {})
        if freshness.get("stock_missing"):
            gaps.append("stock_bars_missing")
        if freshness.get("etf_missing"):
            gaps.append("etf_bars_missing")
    if event["stage"] == "review.daily" and any(
            row["insufficient_evidence"] for row in
            value["review"]["attribution"]["violation_coverage"].values()):
        gaps.append("rule_evidence_missing")
    if event["stage"] == "layer0.scan" and value["scan_stats"]["rejected"]["nodata"]:
        gaps.append("scanner_inputs_missing")
    if event["stage"] == "decision.debate" and any(
            row["status"] != "filing_verified" for row in value["company_evidence"]["candidates"].values()):
        gaps.append("candidate_sources_missing")
    return {"stage": event["stage"], "execution": event["status"], "output_ref": event["output_ref"],
            "io_attempted": len(calls), "io_returned": len(calls) - failed, "io_failed": failed,
            "evidence_status": "degraded" if failed or gaps else "recorded", "gaps": gaps,
            "elapsed_ms": value["scan_elapsed_ms"] if event["stage"] == "layer0.scan" else None}


def _audit_events(run: Path, *, through_reports: bool):
    events = [strict_json(line) for line in read_bytes(run, "events.jsonl").splitlines()]
    stage_rows, values, refs = [], {}, {}
    for event in events:
        name = event["stage"]
        if not through_reports and name == "report.final_json":
            break
        if event["kind"] != "end":
            continue
        output = read_snapshot(run, event["output_ref"], run_id=run.name, stage=name, role="output")
        bound_input = (read_snapshot(run, event["input_ref"], run_id=run.name, stage=name, role="input")
                       if name in {"tech.risk", "tech.fusion"} else None)
        stage_rows.append(_stage_record(event, output, bound_input))
        if name in _REQUIRED:
            values[name] = output["value"]
            refs[name] = event["output_ref"]
        if name == "layer0.scan":
            scan_input = read_snapshot(run, event["input_ref"], run_id=run.name, stage=name, role="input")
            values["universe"] = scan_input["universe"]
            refs["scan_universe"] = event["input_ref"]
    return stage_rows, values, refs


def report_audit(run: Path):
    stages, values, refs = _audit_events(run, through_reports=False)
    funnel = candidate_funnel(values["universe"], values["layer0.scan"]["scan_decisions"],
                              values["layer3.tss"]["watchlist"], values["layer4.risk"]["rejections"],
                              values["decision.debate"], values["pipeline.prepare"].get("security_roster"))
    return {"contract": CONTRACT, "observed_through": stages[-1]["stage"], "stages": stages,
            "funnel": funnel, "rule_coverage": values["review.daily"]["review"]["attribution"]["violation_coverage"],
            "evidence_refs": refs}


def verify_audit(run: Path, actual):
    try:
        expected = report_audit(run)
        if actual != expected:
            raise ValueError("Published research audit differs from stage evidence")
        stages, _, _ = _audit_events(run, through_reports=True)
        return {"contract": CONTRACT, "stages": stages,
                "funnel": expected["funnel"]["gates"], "rule_coverage": expected["rule_coverage"],
                "evidence_refs": expected["evidence_refs"]}
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Research audit cannot be reconstructed from the frozen stages") from exc
