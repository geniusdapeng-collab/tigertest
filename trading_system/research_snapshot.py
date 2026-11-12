"""Immutable research scope, facts, conflicts and coverage at one admitted time."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from .research_work import SNAPSHOT_CONTRACT
from .tasks.contracts import value_hash
from .llm.work_semantics import annotation_payload, text_digest


def research_source_index(documents):
    """List every frozen original even when the decision contains no picks."""
    rows, seen = [], set()
    for document in documents:
        raw = document.raw if hasattr(document, "raw") else document
        if raw.doc_id in seen:
            raise ValueError("RESEARCH_SOURCE_INDEX_DUPLICATE_DOCUMENT")
        seen.add(raw.doc_id)
        rows.append({"id": raw.doc_id, "title": raw.title, "url": raw.url,
            "published_at": raw.published or None, "source_id": raw.source,
            "content_sha256": text_digest(raw.content),
            "completeness": raw.meta.get("completeness", "unknown")})
    return rows


def snapshot_manifest(*, work_manifest, policy, docs, frontier, bundle, source_registry):
    identities = [document.raw.doc_id for document in docs]
    if len(identities) != len(set(identities)):
        raise ValueError("RESEARCH_SNAPSHOT_DUPLICATE_DOCUMENT")
    return {"schema": SNAPSHOT_CONTRACT,
        "run_id": work_manifest["run_id"], "tenant_id": work_manifest["tenant_id"],
        "source_sha256": work_manifest["source_sha256"], "policy_sha256": work_manifest["policy_sha256"],
        "input_sha256": work_manifest["input_sha256"], "as_of": work_manifest["as_of"],
        "research_policy": deepcopy(policy), "frontier": deepcopy(frontier),
        "bundle_id": bundle["bundle_id"], "bundle_sha256": bundle["manifest_sha256"],
        "source_registry_sha256": source_registry["sha256"],
        "documents": [{"id": doc.raw.doc_id, "raw_sha256": value_hash(asdict(doc.raw)),
            "content_sha256": text_digest(doc.raw.content),
            "annotation_sha256": value_hash(annotation_payload(doc)),
            "eligibility_sha256": value_hash(doc.eligibility),
            "conflict_ids": sorted({item for row in doc.eligibility.get("claims", [])
                                     for item in row.get("conflict_ids", [])})} for doc in docs],
        "coverage": deepcopy(bundle["gaps"])}


def freeze_research_snapshot(execution, *, docs, frontier, bundle, source_registry):
    store = execution.research_work_store
    value = snapshot_manifest(work_manifest=store.manifest, policy=execution.research_work_policy,
        docs=docs, frontier=frontier, bundle=bundle, source_registry=source_registry)
    ref = store._blob(value, SNAPSHOT_CONTRACT)
    return {"schema": SNAPSHOT_CONTRACT, "manifest_sha256": value_hash(value), "ref": ref}


def verify_research_snapshot(store, reference, *, policy, docs, frontier, bundle, source_registry):
    expected = snapshot_manifest(work_manifest=store.manifest, policy=policy,
        docs=docs, frontier=frontier, bundle=bundle, source_registry=source_registry)
    if (set(reference) != {"schema", "manifest_sha256", "ref"}
            or reference["schema"] != SNAPSHOT_CONTRACT
            or reference["manifest_sha256"] != value_hash(expected)
            or store.read_artifact(reference["ref"], schema=SNAPSHOT_CONTRACT) != expected):
        raise ValueError("RESEARCH_SNAPSHOT_MISMATCH")
    return expected
