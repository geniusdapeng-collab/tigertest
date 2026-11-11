"""Explicit policy for resumable research; historical executions never opt in.

This policy is embedded in the frozen admission. A runtime capability or a
client request cannot upgrade a saved five-stage execution to this contract.
"""
from __future__ import annotations

from copy import deepcopy

RESEARCH_WORK_CONTRACT = "tiger.research-work/v1"
WORK_CLEAN_CONTRACT = "tiger.document-semantics/v1"
EVIDENCE_VIEW_CONTRACT = "tiger.task-evidence-view/v1"
SNAPSHOT_CONTRACT = "tiger.research-snapshot/v1"
SEGMENT_CONTRACT = "tiger.document-semantics-segments/v1"

_POLICY = {
    "contract": RESEARCH_WORK_CONTRACT,
    "clean_contract": WORK_CLEAN_CONTRACT,
    "evidence_view_contract": EVIDENCE_VIEW_CONTRACT,
    "snapshot_contract": SNAPSHOT_CONTRACT,
    "segment_contract": SEGMENT_CONTRACT,
    "segment_max_count": 64,
    "segment_input_token_limit": 262144,
    "segment_context_characters": 0,
    "pending_risk_contract": "tiger.pending-risk/v1",
    "pending_candidates_contract": "tiger.pending-candidates/v1",
    "relations_contract": "tiger.research-relations/v1",
    "parent_attempt_contract": "tiger.parent-attempt/v1",
    "provider_replay_contract": "tiger.parent-provider-replay/v1",
    "rejected_native_contract": "tiger.rejected-native/v1",
    "document_phases": ["extract", "normalize", "validate", "document_review"],
    "identity_owner": "program",
    "format_retry_limit": 1,
    "retry_scope": "parent_run",
    "unknown_dispatch": "reconcile_before_send",
    "source_workers": 4,
    "parser_workers": 4,
    "parser_temporary_bytes": 268435456,
    "parser_queue": "original_deadline",
    "document_workers": 16,
    "analysis_workers": 3,
    "max_active_input_tokens": 1048576,
    "max_active_memory_bytes": 3221225472,
    "lease_seconds": 120,
    "resume_identity": "same_run_source_input_account_deadline",
}

_PREVIOUS_POLICY = {**_POLICY, "parser_workers": 2, "document_workers": 4}
_LEGACY_POLICY = {key: value for key, value in _PREVIOUS_POLICY.items()
                  if key not in {"segment_contract", "segment_max_count",
                                 "segment_input_token_limit",
                                 "segment_context_characters"}}
_ACCEPTED_POLICIES = (_POLICY, _PREVIOUS_POLICY, _LEGACY_POLICY)


def research_work_policy(*, legacy: bool = False) -> dict:
    return deepcopy(_LEGACY_POLICY if legacy else _POLICY)


def validate_research_work_policy(value) -> dict:
    if value not in _ACCEPTED_POLICIES:
        raise ValueError("RESEARCH_WORK_POLICY_MISMATCH")
    return deepcopy(value)


def active_research_work(execution) -> bool:
    value = getattr(execution, "research_work_policy", None)
    if value is None:
        return False
    validate_research_work_policy(value)
    return True


def work_manifest(admission: dict) -> dict:
    from .agent_evidence import digest, json_bytes
    from .tasks.contracts import CONTRACT, validate_manifest
    identity = {key: admission[key] for key in ("run_id", "request_sha256",
                                               "source_sha256", "policy_sha256", "parent", "clock")}
    identity["runtime_input_sha256"] = admission.get("runtime_input_sha256")
    return validate_manifest({"schema": CONTRACT, "tenant_id": "public-usd",
        "run_id": admission["run_id"], "source_sha256": admission["source_sha256"],
        "policy_sha256": admission["policy_sha256"], "input_sha256": digest(json_bytes(identity)),
        "as_of": admission["clock"]["admitted_at_utc"],
        "deadline_at": admission["clock"]["deadline_at_utc"], "format_retry_limit": 1})


def attach_research_work(execution, admission: dict, policy: dict):
    value = policy.get("research_work")
    if value is None:
        return
    from .tasks.store import WorkStore
    if not execution.intelligence or execution.run_id != admission["run_id"]:
        raise ValueError("RESEARCH_WORK_PARENT_MISMATCH")
    scope_hash = admission.get("research_scope_sha256")
    if scope_hash != policy.get("research_scope_sha256"):
        raise ValueError("RESEARCH_SCOPE_POLICY_MISMATCH")
    scope = None
    if scope_hash is not None:
        from .agent_evidence import read_json
        from .research_followup import validate_scope, _time
        scope = validate_scope(read_json(execution.store.root, "research-scope.json"))
        if (scope["manifest_sha256"] != scope_hash
                or _time(admission["clock"]["admitted_at_utc"]) < _time(scope["requested_at"])):
            raise ValueError("RESEARCH_SCOPE_ADMISSION_MISMATCH")
    execution.research_work_policy = validate_research_work_policy(value)
    execution.rejected_native_contract = execution.research_work_policy["rejected_native_contract"]
    execution.research_work_store = WorkStore.create(execution.store.root / "tasks", work_manifest(admission))
    if scope is not None:
        execution.research_scope = scope
