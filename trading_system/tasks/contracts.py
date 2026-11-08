"""Closed identities for the opt-in Tiger work-unit persistence contract.

This contract does not change historical execution events, stage order, model
proofs or account publication. A graph completion is not a paper receipt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math
import re
from typing import Any

from ..agent_evidence import EvidenceInvalid, digest, json_bytes, strict_json

CONTRACT = "tiger.work-units/v1"
RESULT_SCHEMA = "tiger.work-unit-result/v1"
REQUEST_SCHEMA = "tiger.work-unit-request/v1"
OBSERVATION_SCHEMA = "tiger.work-unit-observation/v1"
EVENT_SCHEMA = "tiger.work-unit-event/v1"
PLAN_SCHEMA = "tiger.work-unit-plan/v1"
SEGMENT_SCHEMA = "tiger.document-segments/v1"
DISPOSITIONS = frozenset({"eligible", "irrelevant", "quarantined", "unverifiable", "rejected"})
UNIT_STATES = frozenset({"ready", "blocked", "running", "validated", "failed", "unknown", "cancelled"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


class WorkUnitError(EvidenceInvalid):
    """Value-free task contract or persistence failure with a stable code."""


def identifier(value: object, label: str = "identity") -> str:
    if type(value) is not str or IDENTIFIER.fullmatch(value) is None:
        raise WorkUnitError("CONTRACT_INVALID", "Invalid work-unit " + label)
    return value


def hash_value(value: object, label: str = "hash") -> str:
    if type(value) is not str or HASH.fullmatch(value) is None:
        raise WorkUnitError("CONTRACT_INVALID", "Invalid work-unit " + label)
    return value


def plain_label(value: object, label: str = "label") -> str:
    if (type(value) is not str or not value or len(value) > 256
            or any(ord(char) < 32 for char in value)):
        raise WorkUnitError("CONTRACT_INVALID", "Invalid work-unit " + label)
    return value


def detached(value: object) -> Any:
    """Strict finite JSON copy; never serialize arbitrary objects or reprs."""
    def check_keys(item):
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError
            for child in item.values():
                check_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_keys(child)
    try:
        check_keys(value)
        return strict_json(json_bytes(value))
    except (UnicodeError, TypeError, ValueError, OverflowError, RecursionError):
        raise WorkUnitError("CONTRACT_INVALID", "Work-unit value must be finite UTF-8 JSON") from None


def value_hash(value: object) -> str:
    return digest(json_bytes(detached(value)))


def instant(value: object) -> float:
    if type(value) is not str:
        raise WorkUnitError("CONTRACT_INVALID", "Work-unit time must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = parsed.timestamp()
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0 or not math.isfinite(result):
            raise ValueError
    except (ValueError, OverflowError, OSError):
        raise WorkUnitError("CONTRACT_INVALID", "Work-unit time must be an aware ISO timestamp") from None
    return result


def timestamp_number(value: object) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise WorkUnitError("CONTRACT_INVALID", "Invalid work-unit clock")
    return float(value)


def validate_manifest(value: object) -> dict:
    fields = {"schema", "tenant_id", "run_id", "source_sha256", "policy_sha256",
              "input_sha256", "as_of", "deadline_at", "format_retry_limit"}
    value = detached(value)
    if (type(value) is not dict or set(value) != fields or value["schema"] != CONTRACT
            or type(value["format_retry_limit"]) is not int or value["format_retry_limit"] != 1):
        raise WorkUnitError("CONTRACT_INVALID", "An explicit work-unit v1 manifest is required")
    identifier(value["tenant_id"], "tenant")
    identifier(value["run_id"], "run")
    for name in ("source_sha256", "policy_sha256", "input_sha256"):
        hash_value(value[name], name)
    if instant(value["deadline_at"]) <= instant(value["as_of"]):
        raise WorkUnitError("CONTRACT_INVALID", "Work-unit deadline must follow its frozen as-of")
    return value


def identifiers(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise WorkUnitError("CONTRACT_INVALID", "Invalid work-unit " + label)
    result = tuple(identifier(item, label) for item in value)
    if len(result) != len(set(result)):
        raise WorkUnitError("CONTRACT_INVALID", "Duplicate work-unit " + label)
    return result


@dataclass(frozen=True)
class UnitSpec:
    unit_id: str
    kind: str
    inputs: dict
    contract: dict
    dependencies: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    terminal_documents: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        value = detached(asdict(self))
        identifier(value["unit_id"], "unit")
        identifier(value["kind"], "kind")
        if (type(value["inputs"]) is not dict or type(value["contract"]) is not dict
                or not {"schema", "validator"} <= set(value["contract"])):
            raise WorkUnitError("CONTRACT_INVALID", "Unit inputs and versioned validation contract are required")
        plain_label(value["contract"].get("schema"), "schema contract")
        plain_label(value["contract"].get("validator"), "validator contract")
        for name in ("dependencies", "document_ids", "terminal_documents"):
            value[name] = list(identifiers(value[name], name))
        if not set(value["terminal_documents"]) <= set(value["document_ids"]):
            raise WorkUnitError("CONTRACT_INVALID", "Terminal document is outside this unit")
        return value

    @classmethod
    def from_dict(cls, value: object) -> "UnitSpec":
        value = detached(value)
        fields = {"unit_id", "kind", "inputs", "contract", "dependencies",
                  "document_ids", "terminal_documents"}
        if type(value) is not dict or set(value) != fields:
            raise WorkUnitError("CONTRACT_INVALID", "Invalid saved work-unit specification")
        result = cls(**{**value, **{name: tuple(value[name]) for name in
                     ("dependencies", "document_ids", "terminal_documents")}})
        result.to_dict()
        return result


@dataclass(frozen=True)
class UnitLease:
    tenant_id: str
    run_id: str
    manifest_sha256: str
    unit_id: str
    owner: str
    epoch: int
    until: float


@dataclass(frozen=True)
class UnitResult:
    value: dict
    dispositions: dict


@dataclass(frozen=True)
class AttemptHandle:
    attempt_id: str
    unit_id: str
    ordinal: int
    request_sha256: str
    request_ref: dict
    reused: bool = False


def known_usage(value: object) -> dict | None:
    if value is None:
        return None
    value = detached(value)
    if (type(value) is not dict or set(value) != {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(type(number) is not int or number < 0 for number in value.values())
            or value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]):
        raise WorkUnitError("USAGE_INVALID", "Known model usage must reconcile exactly")
    return value
