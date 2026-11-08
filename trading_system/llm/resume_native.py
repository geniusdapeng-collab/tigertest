"""Replay an already observed attempt without fabricating a provider envelope."""
from __future__ import annotations


def cached_attempt(execution, ordinal, inputs, observation):
    hook = getattr(execution, "model_replay_attempt", None)
    if hook is None:
        return None
    if not callable(hook):
        raise ValueError("MODEL_REPLAY_HOOK_INVALID")
    value = hook(ordinal, inputs)
    if value is None:
        return None
    from .json_observation import validate_attempt
    value = validate_attempt(value, inputs)
    # The context verifies the ledger's original bytes and writes only a new
    # child reference. No new provider observation or charge is invented.
    ref = execution.model_attempt_snapshot(value)
    entry = {"ordinal": ordinal, "observation_ref": ref,
             "diagnostic": value["transport_diagnostic"]}
    observation["attempts"].append(entry)
    observation["retry_used"] = len(observation["attempts"]) == 2
    return value, entry
