"""Shared comparison for model-derived coverage rows.

Source diagnostics are appended to the frozen gap projection by program code.
They therefore must be checked from their event chain, not treated as model
fields and compared byte-for-byte with the pre-diagnostic assessment.
"""
from __future__ import annotations

from ..agent_evidence import EvidenceInvalid

_BASE_FIELDS = ("cell_id", "target_id", "status", "claim_ids", "conflict_ids", "document_ids")


def coverage_base(rows: object, *, stage: str) -> list[dict]:
    if not isinstance(rows, list):
        raise EvidenceInvalid("RESULT_INVALID", "Coverage rows are not an array", stage=stage)
    projected = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceInvalid("RESULT_INVALID", "Coverage row is not an object", stage=stage)
        cell_id = row.get("cell_id")
        if not isinstance(cell_id, str) or not cell_id or cell_id in seen:
            raise EvidenceInvalid("RESULT_INVALID", "Coverage cell identity is invalid", stage=stage)
        seen.add(cell_id)
        identity_lists = {}
        for name in ("claim_ids", "conflict_ids"):
            value = row.get(name, [])
            if (not isinstance(value, list)
                    or any(not isinstance(item, str) or not item for item in value)
                    or len(value) != len(set(value))):
                raise EvidenceInvalid("RESULT_INVALID", "Coverage identity list is invalid", stage=stage)
            identity_lists[name] = value
        document_ids = row.get("document_ids", [])
        if (not isinstance(document_ids, list)
                or any(not isinstance(item, str) or not item for item in document_ids)
                or len(document_ids) != len(set(document_ids))):
            raise EvidenceInvalid("RESULT_INVALID", "Coverage document identity is invalid", stage=stage)
        projected.append({
            "cell_id": cell_id,
            "target_id": row.get("target_id"),
            "status": row.get("status"),
            "claim_ids": identity_lists["claim_ids"],
            "conflict_ids": identity_lists["conflict_ids"],
            "document_ids": document_ids,
        })
    return projected


def compare_coverage_base(actual: object, expected: object, stage: str) -> None:
    if coverage_base(actual, stage=stage) != coverage_base(expected, stage=stage):
        raise EvidenceInvalid(
            "RESULT_INVALID", "Model-derived coverage evidence differs", stage=stage,
        )
