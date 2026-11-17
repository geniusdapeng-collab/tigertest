"""Shared coverage comparison separates model fields from source diagnostics."""
from copy import deepcopy

import pytest

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.llm.coverage_proof import compare_coverage_base


def rows():
    return [{
        "cell_id": "cell:one",
        "target_id": "security:NVDA",
        "status": "missing",
        "claim_ids": ["claim-1"],
        "document_ids": ["doc-1"],
        "source_diagnostics": [{
            "reason": "SOURCE_TIMEOUT",
            "status": "source_unresolved",
            "attempt_refs": ["attempt-1"],
            "document_ids": [],
            "event_sequence": 9,
        }],
        "source_reason": "SOURCE_TIMEOUT",
    }]


def test_legal_source_diagnostic_and_absent_identity_lists_are_model_neutral():
    observed = rows()
    assessment = deepcopy(observed)
    assessment[0].pop("source_diagnostics")
    assessment[0].pop("source_reason")
    assessment[0]["conflict_ids"] = []
    observed[0]["claim_ids"] = ["claim-1"]
    compare_coverage_base(observed, assessment, "intelligence.freeze")


@pytest.mark.parametrize("field,value", [
    ("claim_ids", ["other-claim"]),
    ("conflict_ids", ["other-conflict"]),
    ("status", "present"),
    ("document_ids", ["other-doc"]),
    ("cell_id", "cell:other"),
])
def test_any_model_derived_coverage_tamper_fails(field, value):
    observed = rows()
    assessment = deepcopy(observed)
    observed[0][field] = value
    with pytest.raises(EvidenceInvalid, match="coverage"):
        compare_coverage_base(observed, assessment, "intelligence.freeze")


def test_diagnostic_attempt_and_event_sequence_are_checked_outside_model_compare():
    observed = rows()
    assessment = deepcopy(observed)
    for row in (observed, assessment):
        row[0].pop("source_diagnostics")
        row[0].pop("source_reason")
    assert observed == assessment
    # Source event binding is intentionally verified by the event-chain
    # projection; model comparison must not reject a legal diagnostic row.
    compare_coverage_base(rows(), assessment, "intelligence.freeze")
