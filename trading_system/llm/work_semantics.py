"""Source-bound extraction, deterministic validation and document review.

Document identity belongs to the work ledger. The model returns annotations
only; no guessed source ID, repaired number or inferred currency is accepted.
Original native replies and source objects remain separate immutable evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from ..research_work import WORK_CLEAN_CONTRACT, EVIDENCE_VIEW_CONTRACT
from ..search.credibility import evidence_for
from ..search.event_identity import validate_structured_events, _resolve_spans
from ..search.models import RawDocument, StructuredDocument
from .deepseek_required import validate_schema
from .required_schemas import clean_schema, PLAIN_TEXT_PATTERN, PUBLIC_TEXT_PATTERN

_TEXT = r"^(?!\s*$)[^\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+$"
_CHECKS = ("subjects", "time_and_period", "amounts_units_currency", "counterevidence",
           "citations", "material_facts")
WORK_MAX_DOCUMENT_TICKERS = 64
_ANNOTATION_FIELDS = ("tickers", "sentiment", "sentiment_score", "events", "summary_zh",
                      "relevance", "subjects", "evidence_spans", "event_records", "claims")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _allow_plain_comparisons(value):
    if isinstance(value, dict):
        result = {key: _allow_plain_comparisons(item) for key, item in value.items()}
        if result.get("pattern") in {PLAIN_TEXT_PATTERN, PUBLIC_TEXT_PATTERN}:
            result["pattern"] = _TEXT
        return result
    if isinstance(value, list):
        return [_allow_plain_comparisons(item) for item in value]
    return value


def extraction_schema(target_ids, *, limits: dict | None = None) -> dict:
    annotation = deepcopy(clean_schema(["program-owned"], structured=True,
                                      target_ids=target_ids)["properties"]["documents"]["items"])
    # The historical batch contract keeps its eight-ticker cap. The new
    # per-document contract must not force a source roundup to hide issuers.
    annotation["properties"]["tickers"]["maxItems"] = WORK_MAX_DOCUMENT_TICKERS
    del annotation["properties"]["id"]
    annotation["required"].remove("id")
    if limits is not None:
        allowed = {"evidence_spans", "subjects", "event_records", "claims"}
        if type(limits) is not dict or set(limits) != allowed:
            raise ValueError("WORK_ANNOTATION_LIMITS_INVALID")
        for name, maximum in limits.items():
            if type(maximum) is not int or maximum < 1:
                raise ValueError("WORK_ANNOTATION_LIMITS_INVALID")
            annotation["properties"][name]["maxItems"] = maximum
    return _object({"annotation": _allow_plain_comparisons(annotation)})


def review_schema() -> dict:
    text = {"type": "string", "minLength": 1, "maxLength": 1000, "pattern": _TEXT}
    findings = {"type": "array", "items": text, "minItems": 0, "maxItems": 64,
                "uniqueItems": True}
    return _object({"complete": {"type": "boolean"},
                    "checks": _object({key: {"type": "boolean"} for key in _CHECKS}),
                    "omissions": deepcopy(findings), "contradictions": deepcopy(findings),
                    "notes": text})


def validate_raw_documents(docs) -> list[RawDocument]:
    if not isinstance(docs, (list, tuple)):
        raise ValueError("RESEARCH_DOCUMENTS_REQUIRED")
    seen = set()
    for doc in docs:
        if (not isinstance(doc, RawDocument) or not isinstance(doc.doc_id, str) or not doc.doc_id
                or not isinstance(doc.content, str) or not doc.content.strip()):
            raise ValueError("RESEARCH_ORIGINAL_REQUIRED")
        if doc.doc_id in seen:
            raise ValueError("DUPLICATE_DOCUMENT_ID")
        seen.add(doc.doc_id)
    # Identical stories from different source/version IDs are independent work
    # items. Corroboration determines origin dependence later, never here.
    return list(docs)


def source_payload(raw: RawDocument) -> dict:
    validate_raw_documents([raw])
    return {"id": raw.doc_id, "source": raw.source, "title": raw.title, "url": raw.url,
            "published": raw.published, "content": raw.content,
            "content_sha256": text_digest(raw.content),
            "source_context": {key: deepcopy(raw.meta[key]) for key in (
                "completeness", "text_kind", "source_times", "authority") if key in raw.meta}}


def annotation_payload(document: StructuredDocument) -> dict:
    return {key: deepcopy(getattr(document, key)) for key in _ANNOTATION_FIELDS}


def normalize_annotation(raw, native, target_ids, *, limits=None) -> StructuredDocument:
    validate_raw_documents([raw])
    try:
        validate_schema({"annotation": native}, extraction_schema(target_ids, limits=limits))
    except Exception as error:
        from ..redline import LLMUnavailable
        if not isinstance(error, (LLMUnavailable, TypeError, ValueError)):
            raise
        raise ValueError("WORK_OUTPUT_SCHEMA_MISMATCH") from error
    document = StructuredDocument(raw=raw, evidence=evidence_for(raw), **deepcopy(native))
    # Character offsets are model-supplied data, not semantic authority. The
    # provider is unreliable at counting Python characters (observed in the
    # real 237-original run), so bind every span from its unique exact quote.
    # A quote that is missing or ambiguous still fails closed below.
    for span in document.evidence_spans:
        span["start"] = span["end"] = None
    spans, errors = _resolve_spans(document)
    if errors:
        raise ValueError("WORK_SPAN_BINDING_INVALID:" + ",".join(sorted(set(errors.values()))))
    # Fill only the unique exact location already proved above. The response
    # stays unchanged and no quote or financial literal is rewritten.
    for span in document.evidence_spans:
        span["start"], span["end"] = spans[span["id"]]["start"], spans[span["id"]]["end"]
    return document


def namespace_segment_annotation(value, segment_number: int, global_offset: int) -> dict:
    """Program-bind segment IDs and map every span into parent character coordinates."""
    if type(value) is not dict or type(segment_number) is not int or segment_number < 1:
        raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
    if type(global_offset) is not int or global_offset < 0:
        raise ValueError("WORK_SEGMENT_OFFSET_INVALID")
    result = deepcopy(value)
    prefix = f"seg{segment_number}-"
    for span in result.get("evidence_spans", []):
        if type(span.get("id")) is not str:
            raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        span["id"] = prefix + span["id"]
        if span.get("start") is not None:
            span["start"] += global_offset
        if span.get("end") is not None:
            span["end"] += global_offset
    rename = {}
    for subject in result.get("subjects", []):
        original = subject.get("id")
        if type(original) is not str:
            raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        subject["id"] = prefix + original
        rename[original] = subject["id"]
        subject["span_ids"] = [prefix + item for item in subject["span_ids"]]
    event_rename = {}
    for event in result.get("event_records", []):
        original = event.get("id")
        if type(original) is not str:
            raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        event["id"] = prefix + original
        event_rename[original] = event["id"]
        event["subject_ids"] = [rename.get(item, prefix + item) for item in event["subject_ids"]]
        for key in ("report_period", "event_time", "effective_time"):
            event[key]["span_ids"] = [prefix + item for item in event[key]["span_ids"]]
        event["source_span_ids"] = [prefix + item for item in event["source_span_ids"]]
    claim_rename = {}
    for claim in result.get("claims", []):
        original = claim.get("id")
        if type(original) is not str:
            raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        claim["id"] = prefix + original
        claim_rename[original] = claim["id"]
    for claim in result.get("claims", []):
        claim["event_id"] = event_rename.get(claim["event_id"], prefix + claim["event_id"])
        claim["source_span_ids"] = [prefix + item for item in claim["source_span_ids"]]
        if claim.get("supersedes_claim_id") is not None:
            claim["supersedes_claim_id"] = claim_rename.get(
                claim["supersedes_claim_id"], prefix + claim["supersedes_claim_id"])
    return result


def merge_segment_annotations(raw, annotations, *, summary_zh, limits, target_ids):
    """Deterministically join every validated segment without dropping typed facts."""
    validate_raw_documents([raw])
    if not isinstance(annotations, list) or not annotations:
        raise ValueError("WORK_SEGMENTS_REQUIRED")
    rows = []
    for value in annotations:
        if type(value) is not dict:
            raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        row = deepcopy(value)
        for key in _ANNOTATION_FIELDS:
            if key not in row:
                raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        if any(type(row.get(name)) is not list for name in (
                "tickers", "events", "subjects", "evidence_spans", "event_records", "claims")):
            raise ValueError("WORK_SEGMENT_ANNOTATION_INVALID")
        rows.append(row)
    tickers, events = [], []
    for row in rows:
        tickers.extend(item for item in row["tickers"] if item not in tickers)
        events.extend(item for item in row["events"] if item not in events)
    weights = [max(1, int(row.get("relevance", 1))) for row in rows]
    score = sum(float(row.get("sentiment_score", 0)) * weight for row, weight in zip(rows, weights, strict=True)) / sum(weights)
    score = max(-1.0, min(1.0, round(score, 6)))
    sentiment = "positive" if score > .1 else "negative" if score < -.1 else "neutral"
    merged = {
        "tickers": tickers,
        "sentiment": sentiment,
        "sentiment_score": score,
        "events": events,
        "summary_zh": summary_zh,
        "relevance": max(1, min(5, max(int(row.get("relevance", 1)) for row in rows))),
        "subjects": [item for row in rows for item in row["subjects"]],
        "evidence_spans": [item for row in rows for item in row["evidence_spans"]],
        "event_records": [item for row in rows for item in row["event_records"]],
        "claims": [item for row in rows for item in row["claims"]],
    }
    return normalize_annotation(raw, merged, target_ids, limits=limits)


def summary_schema() -> dict:
    return _object({"summary_zh": {"type": "string", "minLength": 1, "maxLength": 600,
                                     "pattern": _TEXT}})


def validate_annotation(document, cutoff, *, security_ids=None, entity_registry=None,
                        source_policies=None) -> dict:
    result = validate_structured_events([document], cutoff, security_ids=security_ids,
        entity_registry=entity_registry, source_policies=source_policies)
    row = result["event_identity"]["documents"][document.raw.doc_id]
    errors = [*row.get("span_errors", {}).values(), *row.get("subject_errors", {}).values()]
    if row["status"] == "invalid_annotation":
        errors.append("invalid_annotation")
    errors.extend(item.get("reason", "unverified_event") for item in row.get("events", {}).values()
                  if item["status"] == "unverified_event")
    errors.extend(item.get("reason", "unverified_claim") for item in row.get("claims", {}).values()
                  if item["status"] == "unverified_claim")
    if errors:
        raise ValueError("WORK_ANNOTATION_INVALID:" + ",".join(sorted(set(errors))))
    return {"schema": "tiger.document-validation/v1", "document_id": document.raw.doc_id,
            "source_sha256": text_digest(document.raw.content),
            "annotation_sha256": digest(annotation_payload(document)), "valid": True,
            "eligibility": deepcopy(document.eligibility), "status": row["status"]}


def validate_document_review(document, review) -> dict:
    validate_schema(review, review_schema())
    if (review["complete"] is not True or not all(review["checks"].values())
            or review["omissions"] or review["contradictions"]):
        raise ValueError("DOCUMENT_REVIEW_FAILED")
    return {"schema": "tiger.document-review/v1", "contract": WORK_CLEAN_CONTRACT,
            "document_id": document.raw.doc_id, "source_sha256": text_digest(document.raw.content),
            "annotation_sha256": digest(annotation_payload(document)),
            "review_sha256": digest(review), "complete": True}


def _selected_ranges(document):
    merged = []
    spans, errors = _resolve_spans(document)
    if errors:
        raise ValueError("WORK_VIEW_SPAN_INVALID")
    for start, end in sorted((row["start"], row["end"]) for row in spans.values()):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def task_evidence_view(docs, *, compact=False) -> dict:
    validate_raw_documents([document.raw for document in docs])
    rows, manifests = [], []
    for document in docs:
        raw = document.raw
        selected = _selected_ranges(document) if compact else [[0, len(raw.content)]]
        omitted, cursor = [], 0
        for start, end in selected:
            if cursor < start:
                omitted.append([cursor, start])
            cursor = end
        if cursor < len(raw.content):
            omitted.append([cursor, len(raw.content)])
        row = {key: value for key, value in source_payload(raw).items() if key != "content"}
        row.update(annotation_payload(document), eligibility=deepcopy(document.eligibility))
        if compact:
            row["source_spans"] = [{"start": start, "end": end, "quote": raw.content[start:end]}
                                   for start, end in selected]
        else:
            row["content"] = raw.content
        rows.append(row)
        manifests.append({"document_id": raw.doc_id, "source_sha256": text_digest(raw.content),
            "raw_document_sha256": digest(asdict(raw)), "annotation_sha256": digest(annotation_payload(document)),
            "selected_ranges": selected, "omitted_ranges": omitted,
            "selection_reason": "all_annotation_spans_including_counterevidence" if compact else "complete_original",
            "raw_characters": len(raw.content), "retained_claim_ids": [item["id"] for item in document.claims]})
    return {"contract": EVIDENCE_VIEW_CONTRACT, "view": "validated_facts" if compact else "complete_original",
            "documents": rows, "selection_manifest": manifests}
