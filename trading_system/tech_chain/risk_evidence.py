"""Evidence availability for the scored risk dimension of strict research."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import math

from ..search.event_identity import target_evidence_coverage
from ..search.ontology import ASSESSMENT_STATUSES
from .universe import TECH_SUBCHAINS

SOURCE_PATH = "trading_system/tech_chain/risk_evidence.py"
CONTRACT = "tiger.tech-risk-evidence.v1"
TARGET_CONTRACT = "tiger.tech-risk-evidence.v2"


def risk_coverage(documents, *, chain_ids=None, assessments=None, alerts=None) -> dict:
    """Keep v1 counts unchanged; v2 separates availability, assessment and alerts.

    A single unrelated document cannot establish coverage for six chains. A
    known, evidenced alert survives an incomplete overall assessment and still
    suppresses the relevant chain bonus. Only ``assessed`` owns a risk score.
    """
    if type(documents) is int:
        if documents < 0 or assessments is not None or alerts is not None or chain_ids is not None:
            raise ValueError("Invalid legacy risk coverage")
        return {"contract": CONTRACT, "scorable_documents": documents,
                "status": "available" if documents else "insufficient_evidence"}
    if not isinstance(documents, list):
        raise ValueError("Risk coverage requires evidence documents")
    chains = list(TECH_SUBCHAINS if chain_ids is None else chain_ids)
    if (not chains or len(chains) != len(set(chains))
            or any(chain not in TECH_SUBCHAINS for chain in chains)):
        raise ValueError("Invalid risk coverage targets")
    cells = target_evidence_coverage(documents, chains)
    sources = {chain: cells[chain]["source_ids"] for chain in chains}
    targets = {chain: {"status": cells[chain]["status"],
                       "source_ids": sources[chain], "assessment_source_ids": [],
                       "risk_level": None, "known_alert_level": None,
                       "conflict_ids": cells[chain]["conflict_ids"]}
               for chain in chains}
    known = {chain: [] for chain in chains}
    for observed in alerts or []:
        alert = asdict(observed) if is_dataclass(observed) else observed
        if not isinstance(alert, dict) or alert.get("chain_id") not in targets:
            raise ValueError("Risk alert target is invalid")
        chain, level, refs = alert["chain_id"], alert.get("severity"), alert.get("evidence_ids")
        if (type(level) not in {int, float} or not math.isfinite(level) or not 1 <= level <= 10
                or not isinstance(refs, list) or not refs or len(refs) != len(set(refs))
                or not set(refs) <= set(sources[chain])):
            raise ValueError("Risk alert requires its own qualified target evidence")
        known[chain].append(level)
    for chain in chains:
        if known[chain]:
            targets[chain]["known_alert_level"] = max(known[chain])
    if assessments is not None:
        if (not isinstance(assessments, list) or len(assessments) != len(chains)
                or any(not isinstance(row, dict) for row in assessments)
                or {row.get("chain_id") for row in assessments} != set(chains)):
            raise ValueError("Every risk target must be assessed exactly once")
        for row in assessments:
            chain = row["chain_id"]
            status, level, refs = row.get("status"), row.get("risk_level"), row.get("evidence_ids")
            if (status not in ASSESSMENT_STATUSES or not isinstance(refs, list)
                    or len(refs) != len(set(refs)) or not set(refs) <= set(sources[chain])):
                raise ValueError("Risk assessment source ownership is invalid")
            if status == "assessed":
                if targets[chain]["conflict_ids"]:
                    raise ValueError("Conflicting target evidence cannot receive a resolved risk score")
                if (not refs or type(level) not in {int, float} or not math.isfinite(level)
                        or level != max(known[chain], default=0)):
                    raise ValueError("Complete risk assessment differs from evidenced alerts")
                if not known[chain]:
                    targets[chain]["known_alert_level"] = 0
            elif level is not None:
                raise ValueError("Unknown or conflicted risk score must be null")
            if not sources[chain] and status != "insufficient_evidence":
                raise ValueError("Risk assessment has no relevant evidence")
            targets[chain].update(status=status, risk_level=level, assessment_source_ids=refs)
    available = sum(bool(row["source_ids"]) for row in targets.values())
    return {"contract": TARGET_CONTRACT,
            "scorable_documents": len({source for owned in sources.values() for source in owned}),
            "status": ("available" if available == len(chains) else
                       "partial" if available else "insufficient_evidence"),
            "targets": targets}
