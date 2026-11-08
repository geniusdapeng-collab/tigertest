"""Source-bound adverse leads, kept distinct from verified scoring evidence.

This conservative admission rule does not decide whether a rumor is true or
invent its severity. An unresolved, relevant adverse claim keeps its existing
required research question open and therefore cannot authorize new exposure.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from .llm.work_semantics import text_digest
from .search.event_identity import _document_binding
from .tasks.contracts import value_hash
from .tech_chain.universe import TECH_SUBCHAINS

CONTRACT = "tiger.pending-risk/v1"
_MATERIAL_EVENTS = {"earnings", "guidance", "litigation", "policy", "supply_chain", "capacity", "mna", "macro"}
_FUTURE = {"future_publication", "future_report_period"}


def _signature(event, claim):
    # Exact typed identity and financial literals only. Similar titles,
    # websites or a model's confidence do not prove the same event.
    return value_hash({"subjects": sorted(event["subject_ids"]),
        **{key: event.get(key) for key in ("event_type", "report_period", "event_time", "effective_time")},
        **{key: claim.get(key) for key in ("predicate", "value", "unit", "currency", "relation")}})


def _targets(event, plan):
    values = [("chain:" if target in TECH_SUBCHAINS else "sector:") + target
              for target in event.get("target_ids", [])]
    values.extend("security:" + identity for identity in event.get("tradable_security_ids", []))
    if event.get("event_type") in {"macro", "policy"}:
        values.append("global:macro")
    allowed = {cell["target_id"] for cell in plan["coverage_cells"]}
    return sorted(set(values) & allowed)


def pending_risk_leads(docs, plan):
    cutoff = datetime.fromisoformat(plan["as_of"].replace("Z", "+00:00"))
    if cutoff.utcoffset() is None:
        raise ValueError("RISK_AS_OF_REQUIRED")
    deadline = cutoff + timedelta(seconds=plan["limits"]["deadline_seconds"])
    rows, qualified, excluded_future = [], {}, 0
    for document in docs:
        eligibility = document.eligibility
        if (eligibility.get("document_binding_sha256") != _document_binding(document)
                or eligibility.get("source_sha256") != text_digest(document.raw.content)
                or datetime.fromisoformat(eligibility["cutoff"]) != cutoff):
            raise ValueError("RISK_DOCUMENT_BINDING_MISMATCH")
        events = {row["id"]: row for row in eligibility.get("events", [])}
        for claim in eligibility.get("claims", []):
            event = events.get(claim.get("event_id"))
            if event is None or claim.get("status") not in {"span_verified", "verified", "conflicted"}:
                continue
            if event.get("status") in _FUTURE:
                excluded_future += 1
                continue
            signature = _signature(event, claim)
            stable_time = any(event.get(key) for key in ("report_period", "event_time", "effective_time"))
            if "scoring" in claim.get("allowed_uses", []) and not claim.get("conflict_ids"):
                if not stable_time:
                    continue
                qualified.setdefault(signature, []).append({"document_id": document.raw.doc_id,
                    "claim_id": claim["claim_id"], "source_sha256": eligibility["source_sha256"]})
                continue
            trigger = ("source_bound_refutation" if claim.get("relation") == "refutes" else
                       "negative_material_event" if document.sentiment in {"bearish", "mixed"}
                       and document.sentiment_score < 0 and event.get("event_type") in _MATERIAL_EVENTS else None)
            if trigger is None:
                continue
            targets = _targets(event, plan)
            spans = eligibility.get("resolved_spans", {})
            owned = claim.get("source_span_ids", [])
            if not owned or any(key not in spans for key in owned):
                raise ValueError("RISK_SOURCE_SPANS_REQUIRED")
            blocked = sorted(cell["cell_id"] for cell in plan["coverage_cells"]
                if cell.get("required") and cell["target_id"] in targets
                and cell["material_type"] not in {"connection", "issuer_financial"})
            identity = {"document_id": document.raw.doc_id, "claim_id": claim["claim_id"],
                "source_sha256": eligibility["source_sha256"], "as_of": plan["as_of"]}
            rows.append({"lead_id": "lead_" + value_hash(identity)[:32], **identity,
                "trigger": trigger, "state": "unverified", "verified_fact": False,
                "severity": None, "severity_state": "unassessed",
                "verification_priority": "required_before_new_exposure" if blocked else "observe_outside_scope",
                "qualification_reason": event.get("status", "unverified"),
                "target_ids": targets, "blocked_cell_ids": blocked, "deadline_at": deadline.isoformat(),
                "scoring_contribution": 0, "claim": deepcopy(claim),
                "source_spans": [{"id": key, **deepcopy(spans[key])} for key in owned],
                "event_signature": signature, "qualified_support": []})
    for row in rows:
        support = [item for item in qualified.get(row["event_signature"], [])
                   if item["document_id"] != row["document_id"]]
        if support:
            # The original lead retains its original qualification. Only the
            # separate already-qualified source owns a factual claim or score.
            row.update(state="corroborated_by_qualified_source", qualified_support=support,
                       blocked_cell_ids=[], verification_priority="review_qualified_source")
    rows.sort(key=lambda row: row["lead_id"])
    pending = [row for row in rows if row["state"] == "unverified"]
    return {"schema": CONTRACT, "as_of": plan["as_of"],
        "status": "pending_verification" if pending else "qualified_corroboration_observed" if rows else "no_pending_leads_observed",
        "risk_absence_verified": False, "leads": rows, "pending_count": len(pending),
        "blocked_cell_ids": sorted({cell for row in pending for cell in row["blocked_cell_ids"]}),
        "excluded_future_claims": excluded_future}


def assessment_rows(docs, plan):
    from .llm.intelligence_required import assessment_rows as legacy_assessment
    result = legacy_assessment(docs, plan)
    pending = pending_risk_leads(docs, plan)
    by_cell = {}
    for lead in pending["leads"]:
        for cell in lead["blocked_cell_ids"]:
            by_cell.setdefault(cell, []).append(lead["lead_id"])
    for row in result["coverage"]:
        if row["cell_id"] in by_cell:
            row.update(status="conflicted" if row["status"] == "conflicted" else "insufficient_evidence",
                pending_lead_ids=sorted(by_cell[row["cell_id"]]), reason="pending_source_bound_risk_lead")
    return result
