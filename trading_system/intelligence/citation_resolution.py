"""Transparent citation derivatives for one admitted, narrow layout policy.

The model response is never changed. A derived quote may materialize only the
unique continuous original span when model U+0020 replaced original U+00A0.
The caller must still run the ordinary exact citation and semantic-scope gates.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .contracts import IntelligenceError, digest


CONTRACT = "tiger.topic-citation-resolution/nbsp-to-original/v1"
ARTIFACT_SCHEMA = "tiger.topic-analysis-derivative/v1"


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _quote_span(quote, document):
    text = document["text"]
    if not isinstance(quote, str) or not quote.strip():
        raise IntelligenceError("CITATION_INVALID")
    if quote in text:
        return quote, None
    # One codepoint becomes one codepoint; never collapse or delete spacing,
    # normalize letters, join lines, or search another document.
    normalized = text.replace("\u00a0", " ")
    needle = quote.replace("\u00a0", " ")
    start = normalized.find(needle)
    if start < 0 or normalized.find(needle, start + 1) >= 0:
        raise IntelligenceError("CITATION_INVALID")
    end = start + len(quote)
    original = text[start:end]
    changes = []
    for offset, (model_character, original_character) in enumerate(zip(quote, original)):
        if model_character == original_character:
            continue
        if model_character != " " or original_character != "\u00a0":
            raise IntelligenceError("CITATION_INVALID")
        changes.append({"quote_codepoint_offset": offset,
                        "model_codepoint": "U+0020", "original_codepoint": "U+00A0"})
    if not changes or len(original) != len(quote) or original not in text:
        raise IntelligenceError("CITATION_INVALID")
    return original, {"document_id": document["id"], "source_text_sha256": text_hash(text),
                      "model_quote": quote, "model_quote_sha256": text_hash(quote),
                      "resolved_quote": original, "resolved_quote_sha256": text_hash(original),
                      "start_codepoint": start, "end_codepoint": end,
                      "start_byte": len(text[:start].encode()),
                      "end_byte": len(text[:end].encode()), "changes": changes}


def derive_analysis(raw_analysis, documents):
    """Return a separate derivative plus a reproducible ordered mapping list.

    Schema/type/scope validation belongs to the existing model and exact gates.
    This function does not certify facts, completeness, permissions or subjects.
    """
    by_id = {document["id"]: document for document in documents}
    if len(by_id) != len(documents):
        raise IntelligenceError("CITATION_INVALID")
    analysis, mappings = deepcopy(raw_analysis), []
    for claim_index, claim in enumerate(analysis["conclusions"]):
        for relation in ("supports", "refutes"):
            for index, citation in enumerate(claim[relation]):
                document = by_id.get(citation["document_id"])
                if document is None:
                    raise IntelligenceError("CITATION_INVALID")
                quote, mapping = _quote_span(citation["quote"], document)
                if mapping is not None:
                    mapping.update(claim_index=claim_index, claim_id=claim["id"],
                                   relation=relation, reference_index=index)
                    mappings.append(mapping)
                    citation["quote"] = quote
    return analysis, mappings


def derivative_artifact(job, manifest, documents, raw_analysis, proof):
    """Build all provenance from originals and an already verified raw proof."""
    if job["policy"].get("citation_resolution_contract") != CONTRACT:
        raise IntelligenceError("POLICY_DENIED", "Citation derivatives were not admitted")
    if proof.get("stage") != "sector.narrative" or proof.get("parsed") != raw_analysis:
        raise IntelligenceError("HASH_MISMATCH", "Citation derivative differs from its original analysis")
    analysis, mappings = derive_analysis(raw_analysis, documents)
    if not mappings:
        return analysis, None
    resolution = {"schema": CONTRACT, "job_id": job["job_id"],
                  "job_request_sha256": job["request_sha256"],
                  "policy_sha256": job["policy_sha256"],
                  "source_fingerprint_sha256": job["source_fingerprint"]["sha256"],
                  "snapshot_id": manifest["snapshot_id"], "snapshot_sha256": digest(manifest),
                  "request_sha256": proof["request_sha256"], "response_id": proof["response_id"],
                  "transport_response_sha256": proof["transport_response_sha256"],
                  "canonical_proof_sha256": digest(proof),
                  "raw_analysis_sha256": digest(raw_analysis), "derived_analysis_sha256": digest(analysis),
                  "document_scope": [{"id": d["id"], "text_sha256": text_hash(d["text"])} for d in documents],
                  "mappings": mappings}
    resolution["sha256"] = digest(resolution)
    artifact = {"schema": ARTIFACT_SCHEMA, "analysis": analysis, "resolution": resolution}
    artifact["sha256"] = digest(artifact)
    return analysis, artifact
