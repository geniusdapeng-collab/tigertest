"""Offline migration diagnosis of frozen native-search research documents.

Old annotations and provider text are never silently upgraded into typed facts
or original-page evidence. Optional external typed annotations are evaluated as
declared inputs, without certifying their model provenance or granting action.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import fields
from datetime import datetime
import hashlib

from .agent_evidence import EvidenceInvalid, read_bytes, read_json, read_snapshot, strict_json, verify_events
from .intelligence import default_registry
from .search.event_identity import validate_structured_events
from .search.models import RawDocument, StructuredDocument


def replay_native_run(root, annotations=None):
    admission = read_json(root, "admission.json")
    policy = read_json(root, "policy.json")
    cutoff = datetime.fromisoformat(admission["clock"]["admitted_at_utc"])
    if cutoff.utcoffset() is None:
        raise EvidenceInvalid("RESULT_INVALID", "Replay cutoff needs an aware frozen admission")
    event_check = verify_events(root, root.name, policy["stages"])
    events = [strict_json(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    by_stage = {row["stage"]: row for row in events if row["kind"] == "end"}
    def stage(name):
        return read_snapshot(root, by_stage[name]["output_ref"], run_id=root.name,
                             stage=name, role="output")["value"]
    collected = stage("search.collect")["documents"]
    previous = stage("clean.cross_validate")["stats"].get("event_identity", {}).get("documents", {})
    prepared = stage("pipeline.prepare")
    roster = prepared.get("security_roster", {}).get("symbols", {})
    if not roster:
        roster = {symbol: "unverified-roster:" + symbol for symbol in prepared["universe"]}
    supplied = annotations or {"schema": "tiger.typed-replay-input/v1", "documents": []}
    if (set(supplied) != {"schema", "documents"} or supplied["schema"] != "tiger.typed-replay-input/v1"
            or not isinstance(supplied["documents"], list)):
        raise EvidenceInvalid("INVALID_REQUEST", "Invalid typed replay annotation input")
    annotated = {}
    for row in supplied["documents"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in annotated:
            raise EvidenceInvalid("INVALID_REQUEST", "Replay annotations need unique original document ids")
        annotated[row["id"]] = row
    docs, original_refs = [], {}
    for packet in collected:
        if (not isinstance(packet, dict) or packet.get("__type__") != "dataclass"
                or packet.get("class") != "trading_system.search.models.RawDocument"):
            raise EvidenceInvalid("RESULT_INVALID", "Frozen replay input is not a native document")
        raw = RawDocument(**packet["fields"])
        if raw.doc_id in original_refs:
            raise EvidenceInvalid("RESULT_INVALID", "Frozen replay documents contain duplicate identities")
        content_hash = hashlib.sha256(raw.content.encode()).hexdigest()
        original_refs[raw.doc_id] = {"source_sha256": content_hash, "characters": len(raw.content)}
        # V1 explicitly did not observe the source website. Preserve that fact
        # even when the provider called its own text a full article.
        raw = RawDocument(**{**packet["fields"], "meta": {**raw.meta,
            "text_kind": "provider_text", "completeness": "unknown"}})
        row = annotated.get(raw.doc_id)
        if row is None:
            docs.append(StructuredDocument(raw=raw, degraded=True))
            continue
        expected_fields = {field.name for field in fields(StructuredDocument)} - {
            "raw", "evidence", "corroborated", "eligibility", "degraded", "annotation_contract"}
        if (set(row) != {"id", "source_sha256", "annotation"}
                or row["source_sha256"] != content_hash
                or not isinstance(row["annotation"], dict) or set(row["annotation"]) != expected_fields):
            raise EvidenceInvalid("INVALID_REQUEST", "Replay annotation does not bind the exact frozen original")
        docs.append(StructuredDocument(raw=raw, **row["annotation"]))
    if set(annotated) - original_refs.keys():
        raise EvidenceInvalid("INVALID_REQUEST", "Replay contains annotations for a different run")
    stats = validate_structured_events(docs, cutoff, security_ids=roster,
        entity_registry={"securities": roster, "entities": {}},
        source_policies=default_registry().public_manifest())
    rows = []
    for doc in docs:
        current = stats["event_identity"]["documents"][doc.raw.doc_id]
        old = previous.get(doc.raw.doc_id, {})
        blockers = ["ORIGINAL_WEBSITE_BODY_NOT_CAPTURED"]
        if doc.degraded:
            blockers.append("NO_FRESH_TYPED_ANNOTATION")
        if doc.raw.fetched_at > cutoff.timestamp():
            blockers.append("MUTABLE_VERSION_NOT_OBSERVED_BEFORE_CUTOFF")
        rows.append({"document_id": doc.raw.doc_id, "title": doc.raw.title, "url": doc.raw.url,
            **original_refs[doc.raw.doc_id], "previous_status": old.get("status", "unrecorded"),
            "typed_status": current["status"], "blockers": blockers,
            "events": current["events"], "claims": current.get("claims", {}),
            "scorable": doc.corroborated, "action_authorized": False})
    return {"schema": "tiger.native-intelligence-replay/v1", "mode": "frozen_raw_replay",
        "source_run_id": root.name, "cutoff": cutoff.isoformat(), "source_events": event_check,
        "documents": rows, "document_count": len(rows),
        "characters": sum(row["characters"] for row in rows),
        "typed_status_counts": dict(Counter(row["typed_status"] for row in rows)),
        "previous_status_counts": dict(Counter(row["previous_status"] for row in rows)),
        "supplied_annotations": len(annotated), "model_provenance_verified": False,
        "production_acceptance": "unverified", "network_requests": 0,
        "limitations": ["No source fetch, semantic model or account execution occurs during replay.",
            "V1 provider text cannot become verified website full text by adding annotations.",
            "External annotations have declared provenance only, without a new model receipt.",
            "This diagnoses the captured corpus and does not measure provider recall or improve old run permissions."]}
