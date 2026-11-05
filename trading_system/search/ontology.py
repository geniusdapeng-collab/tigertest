"""Versioned research vocabulary shared by extraction and evidence validation.

This module names facts; it does not infer events, resolve an unfamiliar company
to a security, or grant trading authority. Legacy contracts keep their original
enumerations so saved requests remain independently reproducible.
"""
from __future__ import annotations

import re
import unicodedata

ONTOLOGY_VERSION = "tiger.research-ontology/v2"
ANNOTATION_CONTRACT = "typed-evidence/v2"
EVENT_TYPES = (
    "earnings", "guidance", "mna", "policy", "supply_chain", "capacity",
    "macro", "litigation", "patent", "upgrade", "downgrade",
    "market_observation", "other", "unknown",
)
SUBJECT_TYPES = (
    "issuer", "security", "macro_series", "regulator", "industry", "product",
    "region", "unknown",
)
IDENTIFIER_SCHEMES = ("ticker", "cik", "lei", "series", "regulator", "official_id", "alias")
EVENT_IDENTIFIER_SCHEMES = (
    "sec_accession", "regulation_id", "release_id", "court_case", "contract_id",
    "drug_application",
)
CLAIM_KINDS = (
    "reported_fact", "issuer_disclosure", "official_statistic", "official_policy",
    "forward_guidance", "analysis", "rumor", "unknown",
)
CLAIM_RELATIONS = ("supports", "refutes", "mentions")
PERIOD_KINDS = ("period_end", "quarter", "fiscal_year", "unknown")
TIME_PRECISIONS = ("instant", "date", "unknown")
ASSESSMENT_STATUSES = ("assessed", "insufficient_evidence", "conflicted")
EVIDENCE_USES = ("background", "analysis", "scoring", "action")

# Frozen v1 order and aliases are part of old schema/request/event replay.
LEGACY_ANNOTATION_EVENT_TYPES = (
    "earnings", "guidance", "downgrade", "upgrade", "lawsuit", "supply_chain",
    "policy", "patent", "capacity", "macro", "other",
)
LEGACY_KEY_EVENT_TYPES = (
    "earnings", "mna", "policy", "supply_chain", "capacity", "macro",
    "litigation", "guidance",
)
LEGACY_KEY_EVENT_ALIASES = {
    "earnings": ("earnings", "财报", "业绩"),
    "mna": ("mna", "merger", "acquisition", "takeover", "并购", "收购"),
    "policy": ("policy", "regulation", "export_control", "tariff", "sanction", "政策", "监管", "管制", "关税"),
    "supply_chain": ("supply_chain", "supply", "供应链", "断供"),
    "capacity": ("capacity", "产能", "扩产"),
    "macro": ("macro", "fomc", "cpi", "jobs", "宏观", "非农"),
    "litigation": ("litigation", "lawsuit", "诉讼"),
    "guidance": ("guidance", "指引"),
}
EVENT_ALIASES = {
    **{name: name for name in EVENT_TYPES},
    "lawsuit": "litigation", "merger": "mna", "acquisition": "mna",
    "takeover": "mna", "macro_release": "macro", "policy_action": "policy",
}


def normalized_name(value: str) -> str:
    """Exact Unicode/whitespace normalization, never fuzzy entity matching."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def canonical_event_type(value: str) -> str:
    """Translate a registered annotation alias; unknown values stay unknown."""
    return EVENT_ALIASES.get(normalized_name(value), "unknown")


def legacy_event_type(events: list[str], meta_hint: str = "", *,
                      event_types=LEGACY_KEY_EVENT_TYPES,
                      aliases=LEGACY_KEY_EVENT_ALIASES) -> str | None:
    """Reproduce v1 substring matching without changing its historical policy."""
    def legacy_normalize(value):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip().lower()
    haystacks = [legacy_normalize(value) for value in events]
    if meta_hint:
        haystacks.append(legacy_normalize(meta_hint))
    for canonical in event_types:
        values = (canonical.lower(),) + tuple(value.lower() for value in aliases.get(canonical, ()))
        if any(text and any(value in text for value in values) for text in haystacks):
            return canonical
    return None
