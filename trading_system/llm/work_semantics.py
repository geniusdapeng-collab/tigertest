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


def extraction_schema(target_ids) -> dict:
    annotation = deepcopy(clean_schema(["program-owned"], structured=True,
                                      target_ids=target_ids)["properties"]["documents"]["items"])
    del annotation["properties"]["id"]
    annotation["required"].remove("id")
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


def normalize_annotation(raw, native, target_ids) -> StructuredDocument:
    validate_raw_documents([raw])
    try:
        validate_schema({"annotation": native}, extraction_schema(target_ids))
    except Exception as error:
        from ..redline import LLMUnavailable
        if not isinstance(error, (LLMUnavailable, TypeError, ValueError)):
            raise
        raise ValueError("WORK_OUTPUT_SCHEMA_MISMATCH") from error
    document = StructuredDocument(raw=raw, evidence=evidence_for(raw), **deepcopy(native))
    spans, errors = _resolve_spans(document)
    if errors:
        raise ValueError("WORK_SPAN_BINDING_INVALID:" + ",".join(sorted(set(errors.values()))))
    # Fill only the unique exact location already proved above. The response
    # stays unchanged and no quote or financial literal is rewritten.
    for span in document.evidence_spans:
        span["start"], span["end"] = spans[span["id"]]["start"], spans[span["id"]]["end"]
    return document


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
