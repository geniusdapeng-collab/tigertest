"""Versioned source relationships for new research, with full original scans.

This layer only tightens the established typed event gates. Publisher labels,
model-declared revisions and different domains never grant factual permission.
All originals and claims survive grouping, including contradicting revisions.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from .search.event_identity import (
    _ATTRIBUTION, _DIRECT_HOSTS, _PUBLISHERS, validate_structured_events,
)
from .tasks.contracts import value_hash

CONTRACT = "tiger.research-relations/v1"
_UNAVAILABLE = {"future_publication", "future_report_period", "version_unverified_at_cutoff",
                "unknown_publication", "stale_publication"}
_WITHDRAWAL = re.compile(r"\b(?:this\s+(?:article|report|release)\s+(?:has been|is)\s+"
    r"(?:retracted|withdrawn)|retraction notice|withdrawal notice)\b", re.IGNORECASE)


def _url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _document(doc, cutoff):
    host = (urlsplit(doc.raw.url).hostname or "").lower().rstrip(".")
    publisher = next((item for item in sorted(_DIRECT_HOSTS)
                      if host == item or host.endswith("." + item)), None)
    attributions = []
    for match in _ATTRIBUTION.finditer(doc.raw.content):
        name = next(value for value in match.groups() if value)
        attributions.append({"publisher": _PUBLISHERS[name.lower()],
            "start": match.start(), "end": match.end(), "quote": match.group()})
    notices = [{"start": match.start(), "end": match.end(), "quote": match.group(),
                "status": "pending_verification"} for match in _WITHDRAWAL.finditer(doc.raw.content)]
    source_times = doc.raw.meta.get("source_times", {})
    def time_value(name):
        value = source_times.get(name)
        return deepcopy(value.get("value")) if isinstance(value, dict) else None
    authority = doc.raw.meta.get("authority", {})
    proof = authority.get("proof_ref", {}) if isinstance(authority, dict) else {}
    first = (proof.get("accepted_at") if authority.get("scope") == "sec_filing" else
             proof.get("archive_first_seen_at") if authority.get("proof_type") == "archived_version" else None)
    return {"document_id": doc.raw.doc_id, "url": doc.raw.url, "canonical_url": _url(doc.raw.url),
        "body_sha256": hashlib.sha256(doc.raw.content.encode()).hexdigest(),
        "observed_host": host, "reviewed_publisher": publisher,
        "attributions": attributions, "editorial_notices": notices,
        "times": {"published": time_value("published") or doc.raw.published,
            "first_available": first, "fetched_at": doc.raw.fetched_at,
            "revised_at": time_value("modified"), "decision_as_of": cutoff.isoformat()},
        "availability_statuses": sorted({row["status"] for row in doc.eligibility["events"]}),
        "available_for_current_relations": any(row["status"] not in _UNAVAILABLE
                                               for row in doc.eligibility["events"])}


def _origins(docs, records):
    parents = {doc.raw.doc_id: doc.raw.doc_id for doc in docs}
    keys, edges = {}, []
    def find(identity):
        while parents[identity] != identity:
            parents[identity] = parents[parents[identity]]
            identity = parents[identity]
        return identity
    for doc in sorted(docs, key=lambda value: value.raw.doc_id):
        identity, row = doc.raw.doc_id, records[doc.raw.doc_id]
        # An explicitly unknown domain is only a same-host dependency, never
        # proof of independent journalism or an official source.
        owned = {("publisher", row["reviewed_publisher"] or row["observed_host"]),
                 ("content", row["body_sha256"]), ("url", row["canonical_url"])}
        owned.update(("publisher", item["publisher"]) for item in row["attributions"])
        owned.update(("official_origin", event["origin"]) for event in doc.eligibility["events"]
                     if event.get("origin", "").startswith(("issuer:", "official:")))
        for owned_key in sorted(owned):
            # A later version cannot bridge two earlier origins merely by
            # repeating both. Future/unknown observations stay in the graph
            # but never change pre-cutoff source independence.
            key = (row["available_for_current_relations"], *owned_key)
            if key not in keys:
                keys[key] = identity
                continue
            previous = keys[key]
            left, right = find(identity), find(previous)
            if left != right:
                parents[max(left, right)] = min(left, right)
            edges.append({"left": previous, "right": identity,
                          "basis": key[1], "value": key[2]})
    groups = defaultdict(list)
    for identity in sorted(parents):
        groups[find(identity)].append(identity)
    result, by_document = [], {}
    for members in sorted(groups.values()):
        group_id = "origin_" + value_hash(members)[:32]
        result.append({"origin_group_id": group_id, "document_ids": members,
                       "independence_claimed": False})
        by_document.update({identity: group_id for identity in members})
    return result, by_document, edges


def _tighten(docs, records, origin_ids):
    events = defaultdict(list)
    for doc in docs:
        for event in doc.eligibility["events"]:
            event["origin_group_id"] = origin_ids[doc.raw.doc_id]
            if "scoring" in event["allowed_uses"]:
                events[event["event_id"]].append((doc, event))
    for members in events.values():
        eligible_groups = {origin_ids[doc.raw.doc_id] for doc, event in members
                           if not records[doc.raw.doc_id]["editorial_notices"]}
        keepers = {}
        for doc, event in sorted(members, key=lambda value: (
                bool(records[value[0].raw.doc_id]["attributions"]), value[0].raw.doc_id, value[1]["id"])):
            group = origin_ids[doc.raw.doc_id]
            event["independent_origin_count"] = len(eligible_groups)
            reason = None
            if records[doc.raw.doc_id]["editorial_notices"]:
                reason = "editorial_withdrawal_pending_verification"
            elif event["status"] != "verified_original":
                if len(eligible_groups) < 2:
                    reason = "insufficient_independent_origin_groups"
                elif group in keepers and keepers[group] != doc.raw.doc_id:
                    reason = "dependent_origin_group"
                else:
                    keepers[group] = doc.raw.doc_id
            if reason is not None:
                event["status"] = reason
                event["allowed_uses"] = [use for use in event["allowed_uses"] if use not in {"scoring", "action"}]
                event["action_security_ids"] = []
    for doc in docs:
        by_id = {row["id"]: row for row in doc.eligibility["events"]}
        for claim in doc.eligibility["claims"]:
            event = by_id.get(claim.get("event_id"))
            if event is not None and "scoring" not in event["allowed_uses"]:
                claim["allowed_uses"] = [use for use in claim["allowed_uses"] if use not in {"scoring", "action"}]
                claim["action_security_ids"] = []
                if claim["status"] == "verified":
                    claim["status"] = "span_verified"
            claim["origin_group_id"] = origin_ids[doc.raw.doc_id]


def _revisions(docs):
    claims = {row["claim_id"]: (doc, row) for doc in docs for row in doc.eligibility["claims"]}
    result = []
    for claim_id, (doc, claim) in sorted(claims.items()):
        old = claim.get("supersedes_claim_id")
        if old is None:
            continue
        event = next((row for row in doc.eligibility["events"] if row["id"] == claim.get("event_id")), None)
        available = event is not None and event["status"] not in _UNAVAILABLE
        result.append({"new_claim_id": claim_id, "prior_claim_id": old,
            "prior_claim_present": old in claims,
            "status": "pending_verification" if available else "not_available_at_cutoff",
            "affects_current_qualification": False, "original_preserved": True,
            "original_removed": False, "basis": "model_declared_not_independently_proved"})
    return result


def _conflicts(docs):
    groups = {}
    for doc in docs:
        for claim in doc.eligibility["claims"]:
            for identity in claim["conflict_ids"]:
                row = groups.setdefault(identity, {"conflict_id": identity,
                    "subject_ids": deepcopy(claim["subject_ids"]),
                    "event_identity": claim["event_identity"], "predicate": claim["predicate"],
                    "report_period": claim["report_period"], "unit": claim["unit"], "currency": claim["currency"],
                    "claim_ids": [], "facts": [], "status": "unresolved", "required_action": "verify_original_sources"})
                row["claim_ids"].append(claim["claim_id"])
                row["facts"].append({"document_id": doc.raw.doc_id, "claim_id": claim["claim_id"],
                    "value": claim["value"], "relation": claim["relation"],
                    "source_span_ids": deepcopy(claim["source_span_ids"]), "source_sha256": doc.eligibility["source_sha256"]})
    for row in groups.values():
        row["claim_ids"].sort()
        row["facts"].sort(key=lambda fact: fact["claim_id"])
    return [groups[key] for key in sorted(groups)]


def validate_research_events(docs, cutoff, *, security_ids=None, entity_registry=None, source_policies=None):
    stats = validate_structured_events(docs, cutoff, security_ids=security_ids,
        entity_registry=entity_registry, source_policies=source_policies)
    records = {doc.raw.doc_id: _document(doc, cutoff) for doc in docs}
    groups, origin_ids, edges = _origins(docs, records)
    _tighten(docs, records, origin_ids)
    version_groups = defaultdict(list)
    for identity, row in records.items():
        row["origin_group_id"] = origin_ids[identity]
        version_groups[row["canonical_url"]].append(identity)
    versions = [{"canonical_url": url, "document_ids": sorted(ids), "relation": "observed_distinct_versions",
                 "selection": "point_in_time_qualification_only"}
                for url, ids in sorted(version_groups.items())
                if len({records[identity]["body_sha256"] for identity in ids}) > 1]
    graph = {"schema": CONTRACT, "as_of": cutoff.isoformat(), "documents": records,
        "origin_groups": groups, "dependency_edges": edges, "versions": versions,
        "revisions": _revisions(docs), "conflicts": _conflicts(docs)}
    graph["manifest_sha256"] = value_hash(graph)
    for doc in docs:
        doc.eligibility["research_relations"] = {"schema": CONTRACT, "manifest_sha256": graph["manifest_sha256"],
                                                "origin_group_id": origin_ids[doc.raw.doc_id]}
        eligible = [row for row in doc.eligibility["claims"] if "scoring" in row["allowed_uses"]]
        doc.corroborated = bool(eligible)
        summary = stats["event_identity"]["documents"][doc.raw.doc_id]
        if eligible:
            summary["status"] = "conflicted" if any(row["conflict_ids"] for row in eligible) else "eligible"
        elif summary["status"] != "invalid_annotation" and doc.eligibility["events"]:
            summary["status"] = doc.eligibility["events"][0]["status"]
    stats.update(corroborated=sum(doc.corroborated for doc in docs),
        downgraded=sum(not doc.corroborated for doc in docs),
        scorable_events=sum("scoring" in event["allowed_uses"] for doc in docs for event in doc.eligibility["events"]),
        research_relations=graph)
    return stats
