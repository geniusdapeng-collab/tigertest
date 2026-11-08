"""Evidence-bound semantic review, separate from exact quotation validation.

The model classifies meaning. The program requires complete original-reference
coverage and rejects contradictions in those classifications; it never infers
event truth from keywords, rewrites a conclusion, or changes a citation ID.
"""
from __future__ import annotations

from .contracts import IntelligenceError


CONTRACT = "tiger.topic-semantic-review/v1"
PHASES = ["not_event", "before_event", "after_event", "unknown"]
SYSTEM_SUFFIX = (
    " Produce one claim_checks item for every supplied report conclusion, and one evidence item for every"
    " supports/refutes reference within this review's source scope. Copy its relation, zero-based reference_index,"
    " immutable document_id and exact quote without reassignment. Independently classify the assertion_kind;"
    " a factual assertion disguised as opinion/forecast is a mismatch, not a repair."
    " Explicitly distinguish pre-event announcements, calendars and forecasts (before_event) from an event"
    " having occurred or its observed results (after_event). source_phase describes what the cited source says"
    " about that same event, not its crawl time. A later observation date or an IR calendar does not prove"
    " a meeting already happened. Do not invent dates, event completion, or post-meeting statements."
    " Use unknown when timing or support is unresolved; never call it supported to satisfy accepted."
    " Check the summary too: no unsupported facts, hidden extra conclusions, or false event timing may be"
    " smuggled into summary/changes/conditions. Preserve contrary evidence and explicit unknowns."
    " Respect the immutable topic.max_conclusions if supplied; do not delete sources or unknowns to meet it."
    " Partial RSS/snippets cannot establish facts even with exact quotations. Facts require complete originals."
    " For source_batch review, assess only local references and leave cross-source support unknown if needed;"
    " the global review must settle every conclusion from all independently checked batches."
    " accepted=true never overrides contradicted/unknown global support or an inconsistent event phase."
)


def _obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def review_schema(analysis, documents):
    string = {"type": "string", "minLength": 1}
    boolean = {"type": "boolean"}
    document_ids = [doc["id"] for doc in documents]
    claim_ids = [claim["id"] for claim in analysis["conclusions"]]
    evidence = {"type": "array", "items": _obj({
        "relation": {"enum": ["supports", "refutes"]},
        "reference_index": {"type": "integer", "minimum": 0},
        "document_id": {**string, **({"enum": document_ids} if document_ids else {})},
        "quote": string, "source_phase": {"enum": PHASES}, "rationale": string})}
    if not document_ids:
        evidence["maxItems"] = 0
    checks = {"type": "array", "minItems": len(claim_ids), "maxItems": len(claim_ids),
        "items": _obj({"claim_id": {**string, **({"enum": claim_ids} if claim_ids else {})},
            "assertion_kind": {"enum": ["fact", "opinion", "forecast"]},
            "support_status": {"enum": ["supported", "contradicted", "unknown"]},
            "asserted_phase": {"enum": PHASES}, "temporally_consistent": boolean,
            "rationale": string, "evidence": evidence})}
    return _obj({"accepted": boolean, "issues": {"type": "array", "items": string},
        "summary": string, "summary_supported": boolean, "counterevidence_preserved": boolean,
        "claim_checks": checks})


def validate_review(review, analysis, documents, *, source_batch=False):
    """Verify a real review's structure and reject its explicit contradictions.

    Local batches may defer global support; all originals and all references
    are required again by the final review. A boolean acceptance is insufficient.
    """
    from ..llm.deepseek_required import validate_schema
    from ..redline.model_diagnostics import RequiredModelUnavailable

    try:
        validate_schema(review, review_schema(analysis, documents))
    except RequiredModelUnavailable:
        raise IntelligenceError("REVIEW_CONTRACT_INVALID", "Semantic review is incomplete or outside scope") from None
    docs = {doc["id"]: doc for doc in documents}
    claims = {claim["id"]: claim for claim in analysis["conclusions"]}
    checks = review["claim_checks"]
    if (len(docs) != len(documents) or len(claims) != len(analysis["conclusions"])
            or len({check["claim_id"] for check in checks}) != len(checks)
            or {check["claim_id"] for check in checks} != set(claims)):
        raise IntelligenceError("REVIEW_CONTRACT_INVALID", "Review claim identity must be a bijection")
    if not review["accepted"] or not review["summary_supported"] or not review["counterevidence_preserved"]:
        raise IntelligenceError("REVIEW_REJECTED")
    for check in checks:
        claim = claims[check["claim_id"]]
        if (check["assertion_kind"] != claim["kind"] or check["support_status"] == "contradicted"
                or not source_batch and check["support_status"] != "supported"):
            raise IntelligenceError("REVIEW_REJECTED", "Independent review does not support this assertion")
        if not check["temporally_consistent"]:
            raise IntelligenceError("TEMPORAL_CONTRADICTION")
        expected = {(relation, index): ref for relation in ("supports", "refutes")
            for index, ref in enumerate(claim[relation]) if ref["document_id"] in docs}
        actual = {}
        for evidence in check["evidence"]:
            key = (evidence["relation"], evidence["reference_index"])
            if type(evidence["reference_index"]) is not int or key in actual:
                raise IntelligenceError("REVIEW_CONTRACT_INVALID", "Duplicate or mistyped review reference")
            ref = {name: evidence[name] for name in ("document_id", "quote")}
            document = docs.get(evidence["document_id"])
            if (expected.get(key) != ref or document is None or not evidence["quote"].strip()
                    or evidence["quote"] not in document["text"]):
                raise IntelligenceError("REVIEW_CONTRACT_INVALID", "Review reference differs from the same original")
            if claim["kind"] == "fact" and document.get("completeness") != "complete":
                raise IntelligenceError("INCOMPLETE_FACT_SOURCE")
            actual[key] = evidence
        if set(actual) != set(expected):
            raise IntelligenceError("REVIEW_CONTRACT_INVALID", "Every original reference needs a review")
        if not source_batch and len(expected) != len(claim["supports"]) + len(claim["refutes"]):
            raise IntelligenceError("REVIEW_CONTRACT_INVALID", "Global review cannot omit cited originals")
        supporting = [item["source_phase"] for key, item in actual.items() if key[0] == "supports"]
        if not source_batch and check["asserted_phase"] in {"before_event", "after_event"}:
            if check["asserted_phase"] not in supporting:
                raise IntelligenceError("TEMPORAL_CONTRADICTION", "Event phase has no matching original support")
        if not source_batch and claim["kind"] == "fact" and check["asserted_phase"] == "unknown":
            raise IntelligenceError("TEMPORAL_UNVERIFIED", "Unknown event timing cannot certify a fact")
    return review
