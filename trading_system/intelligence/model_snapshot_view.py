"""A versioned model context for an independently verified full snapshot.

The archive retains and verifies every member, selection receipt and original.
The model needs the evidence context and originals, not repeated event chains.
This projection is not an archive verifier and never projects document fields.
"""
from __future__ import annotations

from copy import deepcopy

from .contracts import IntelligenceError, digest


CONTRACT = "tiger.topic-model-snapshot-view/v1"
_FIELDS = ("snapshot_id", "manifest_sha256", "tenant", "as_of", "cutoff", "registry_hash",
    "topic_ref", "scope", "counts", "gaps", "state", "limits", "selection_policy",
    "revision_policy", "integrity_scope")


def model_snapshot_view(manifest, contract=None):
    """Project only admitted jobs; old requests retain their complete bytes.

    Callers must first use Archive.snapshot()/snapshot_documents(), whose
    original full membership/original/receipt verification remains mandatory.
    The complete record digest also binds omitted proof details to this view.
    """
    if contract is None:
        return manifest
    if contract != CONTRACT:
        raise IntelligenceError("HASH_MISMATCH", "Unknown admitted model snapshot view contract")
    try:
        unsigned = {key: value for key, value in manifest.items()
            if key not in {"snapshot_id", "manifest_sha256"}}
        sha = digest(unsigned)
        if (manifest["schema"] != "topic_snapshot/v1" or manifest["manifest_sha256"] != sha
                or manifest["snapshot_id"] != "snapshot_" + sha[:32]):
            raise ValueError
        return {"schema": CONTRACT, **{key: deepcopy(manifest[key]) for key in _FIELDS},
            "full_manifest_sha256": digest(manifest)}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IntelligenceError("HASH_MISMATCH", "Model snapshot view needs its complete original manifest") from None
