"""Persisted evidence boundary: exact types, immutable snapshots and hostile files."""
from datetime import date
import json
import os

import numpy as np
import pandas as pd
import pytest

from trading_system.agent_evidence import (
    EvidenceInvalid, EvidenceStore, atomic_bytes, atomic_json, digest, file_ref,
    json_bytes, normalize, read_bytes, read_json, read_snapshot, strict_json,
    verify_events, verify_ref,
)
from trading_system.redline.execution import (
    ExecutionContext, ExecutionContractError, ExecutionPolicy, RuntimePaths,
    REQUIRED_POLICY_ID, daily_stage_contract,
)
from trading_system.redline.model_diagnostics import RequiredModelUnavailable


def context(root, *, model_required=False):
    reports = root / "work" / "reports"
    reports.mkdir(parents=True)
    paths = RuntimePaths(
        work_dir=root / "work", reports_dir=reports,
        journal_path=reports / "journal.json", sim_path=reports / "sim.json",
        calibration_path=reports / "calibration.json", options_history_dir=reports / "options",
        governance_path=reports / "governance.jsonl", proposals_dir=reports / "proposals",
        calendar_override_path=root / "work" / "calendar.json",
    )
    return ExecutionContext(
        run_id="run-" + "1" * 32, run_date=date(2026, 10, 20), paths=paths,
        policy=(ExecutionPolicy(policy_id=REQUIRED_POLICY_ID, model_mode="enabled",
                                search_source_names=("deepseek_search",))
                if model_required else ExecutionPolicy(policy_id="test-evidence")),
        store=EvidenceStore(root, "run-" + "1" * 32),
    )


def complete(root):
    execution = context(root)
    for spec in daily_stage_contract():
        with execution.step(spec["name"], {"source": [1, 2]}) as scope:
            if spec["driver"] == "llm":
                scope.passthrough("fixture model unavailable")
            execution.call_io("test.read", {"request": spec["name"]}, lambda: {"value": 12})
            scope.output({"observed": [3, 4]})
    execution.assert_complete()
    return execution


def rewrite_chain(root, mutate):
    rows = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    mutate(rows)
    previous = None
    for row in rows:
        row["previous_event_sha256"] = previous
        row.pop("event_sha256", None)
        row["event_sha256"] = digest(json_bytes(row))
        previous = row["event_sha256"]
    (root / "events.jsonl").write_bytes(b"".join(json_bytes(row) for row in rows))


@pytest.mark.parametrize("raw", [
    '{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}',
    '{"x":1}'.encode("utf-16"), '{"x":1}'.encode("utf-32"),
    b'{"x":"\xff"}', "\ufeff{}",
])
def test_strict_json_rejects_ambiguous_or_non_utf8(raw):
    with pytest.raises((ValueError, UnicodeError)):
        strict_json(raw)


def test_snapshot_preserves_missing_types_and_table_axes(tmp_path):
    value = pd.DataFrame({
        "price": [np.float64(2.5), np.nan], "when": [pd.Timestamp("2026-10-20"), pd.NaT],
    }, index=pd.Index(["AAA", "BBB"], name="ticker"))
    encoded = normalize(value)
    assert encoded["index"] == ["AAA", "BBB"]
    assert encoded["index_names"] == ["ticker"]
    assert encoded["columns"] == ["price", "when"]
    assert encoded["values"][1][0] == {"__type__": "missing", "reason": "nan"}
    assert encoded["values"][1][1] == {"__type__": "missing", "reason": "pandas_na"}
    assert normalize(pd.NA) == {"__type__": "missing", "reason": "pandas_na"}
    assert strict_json(json_bytes(encoded)) == encoded


@pytest.mark.parametrize("value", [object(), {1: "not-string"}, complex(1, 2)])
def test_arbitrary_objects_cannot_be_stringified(value):
    with pytest.raises(TypeError):
        normalize(value)


def test_cycles_fail_and_eager_snapshot_survives_mutation(tmp_path):
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(TypeError, match="Cyclic"):
        normalize(cyclic)
    execution = context(tmp_path)
    values = {"frame": pd.DataFrame({"x": [1, 2]}), "items": [1]}
    with execution.step("daily.preflight", values) as scope:
        values["frame"].loc[0, "x"] = 99
        values["items"].append(2)
        scope.output(values)
        values["items"].append(3)
    row = execution.summary()[0]
    before = read_snapshot(tmp_path, row["input_ref"], run_id=execution.run_id,
                           stage="daily.preflight", role="input")
    after = read_snapshot(tmp_path, row["output_ref"], run_id=execution.run_id,
                          stage="daily.preflight", role="output")
    assert before["items"] == [1]
    assert before["frame"]["values"] == [[1], [2]]
    assert after["value"]["items"] == [1, 2]


def test_complete_stream_verifies_actual_child_refs(tmp_path):
    execution = complete(tmp_path)
    result = verify_events(tmp_path, execution.run_id, daily_stage_contract())
    assert result["stage_count"] == 39
    assert result["event_count"] == 78
    assert len(result["degraded"]) == 5
    assert len(result["evidence_refs"]) >= 39 * 3


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[0].update(stage_index=2),
    lambda rows: rows[0].update(stage_index=True),
    lambda rows: rows[0].update(sequence=True),
    lambda rows: rows[0].update(status="completed"),
    lambda rows: rows[0].update(output_ref=rows[1]["output_ref"]),
    lambda rows: rows[0].update(reason="not running"),
    lambda rows: rows[0].update(extra="unbound"),
    lambda rows: rows[1].update(error_type="OSError"),
    lambda rows: rows[1].update(reason="not passthrough"),
    lambda rows: rows[1].update(status="passthrough", reason="rules cannot passthrough"),
    lambda rows: rows[1].pop("input_ref"),
    lambda rows: rows[2].update(previous_stage_ref=None),
    lambda rows: rows.reverse(),
])
def test_rehashed_malformed_events_still_fail(tmp_path, mutate):
    execution = complete(tmp_path)
    rewrite_chain(tmp_path, mutate)
    with pytest.raises(EvidenceInvalid):
        verify_events(tmp_path, execution.run_id, daily_stage_contract())


def test_missing_or_modified_child_snapshot_fails(tmp_path):
    execution = complete(tmp_path)
    row = execution.summary()[0]
    output = read_snapshot(tmp_path, row["output_ref"], run_id=execution.run_id,
                           stage="daily.preflight", role="output")
    ref = output["io_calls"][0]["output_ref"]
    (tmp_path / ref["path"]).write_bytes(b"{}\n")
    with pytest.raises(EvidenceInvalid, match="hash mismatch"):
        verify_events(tmp_path, execution.run_id, daily_stage_contract())


def rewrite_first_io(root, mutate, error_payload=None, *, use_error=False):
    """Keep every digest/link valid so rejection must come from I/O semantics."""
    rows = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    output = read_json(root, rows[1]["output_ref"]["path"])
    call = output["value"]["io_calls"][0]

    def save(blob):
        content = json_bytes(blob)
        relative = f"evidence/{digest(content)}.json"
        atomic_bytes(root, relative, content)
        return file_ref(root, relative, blob["schema"])

    if use_error:
        child = read_json(root, call.pop("output_ref")["path"])
        child["value"] = error_payload
        call["error_ref"] = save(child)
    mutate(call)
    replacement = save(output)

    def relink(events):
        events[1]["output_ref"] = replacement
        for event in events[2:4]:
            event["previous_stage_ref"] = replacement
    rewrite_chain(root, relink)


@pytest.mark.parametrize("method", ["test.\nread", "test.\rread"])
def test_rehashed_multiline_io_method_is_rejected(tmp_path, method):
    execution = complete(tmp_path)
    rewrite_first_io(tmp_path, lambda call: call.update(method=method))
    with pytest.raises(EvidenceInvalid, match="I/O evidence schema"):
        verify_events(tmp_path, execution.run_id, daily_stage_contract())


@pytest.mark.parametrize("payload", [
    None, [], {},
    {"error_type": "OSError", "message": "External call failed"},
    {"error_type": "OSError", "message": "External call failed",
     "outcome_observed": True, "extra": "unbound"},
    {"error_type": 1, "message": "External call failed", "outcome_observed": True},
    {"error_type": "", "message": "External call failed", "outcome_observed": True},
    {"error_type": "OSError", "message": None, "outcome_observed": True},
    {"error_type": "OSError", "message": "Uncontrolled error body", "outcome_observed": True},
    {"error_type": "OSError", "message": "External call failed", "outcome_observed": 1},
    {"error_type": "OSError", "message": "External call failed", "outcome_observed": "false"},
])
def test_rehashed_malformed_io_error_payload_is_rejected(tmp_path, payload):
    execution = complete(tmp_path)
    rewrite_first_io(tmp_path, lambda _call: None, payload, use_error=True)
    with pytest.raises(EvidenceInvalid, match="I/O error payload"):
        verify_events(tmp_path, execution.run_id, daily_stage_contract())


@pytest.mark.parametrize("observed", [True, False])
def test_actual_controlled_io_error_payload_remains_valid(tmp_path, observed):
    execution = context(tmp_path)
    for spec in daily_stage_contract():
        with execution.step(spec["name"], {"fixture": "controlled IO failure"}) as scope:
            if spec["driver"] == "llm":
                scope.passthrough("fixture model unavailable")
            handle = execution.begin_io("test.read", {"stage": spec["name"]})
            execution.finish_io(handle, error=OSError("private external error"),
                                outcome_observed=observed)
            scope.output({"fallback": "fixture"})
    execution.assert_complete()
    result = verify_events(tmp_path, execution.run_id, daily_stage_contract())
    assert result["stage_count"] == 39
    assert result["event_count"] == 78
    first = execution.summary()[0]
    output = read_snapshot(tmp_path, first["output_ref"], run_id=execution.run_id,
                           stage="daily.preflight", role="output")
    error = read_snapshot(tmp_path, output["io_calls"][0]["error_ref"],
                          run_id=execution.run_id, stage="daily.preflight", role="output")
    assert error == {"error_type": "OSError", "message": "External call failed",
                     "outcome_observed": observed}


def test_required_failure_persists_only_closed_diagnostic_and_remains_terminal(tmp_path):
    execution = context(tmp_path, model_required=True)
    failure = RequiredModelUnavailable("OUTPUT_SCHEMA_MISMATCH", schema_path="/chains/2/heat",
                                       schema_keyword="maximum")
    failure.untrusted_response = "fixture-secret model-output reasoning-content"
    def fail():
        raise failure
    with pytest.raises(RequiredModelUnavailable):
        with execution.step("daily.preflight", {"fixture": "terminal model failure"}):
            execution.call_io("model.deepseek.chat", {"request": "keyless"}, fail)
    snapshots = [json.loads(path.read_text()) for path in (tmp_path / "evidence").glob("*.json")]
    errors = [item["value"] for item in snapshots
              if item["role"] == "output" and "diagnostic" in item.get("value", {})]
    assert errors == [{"error_type": "RequiredModelUnavailable", "message": "External call failed",
                       "outcome_observed": True, "diagnostic": failure.diagnostic}]
    assert "fixture-secret" not in json.dumps(snapshots)
    assert execution.summary()[0]["status"] == "failed"
    with pytest.raises(ExecutionContractError):
        with execution.step("state.reset", {}):
            pytest.fail("A failed execution cannot proceed")
    with pytest.raises(ExecutionContractError):
        execution.assert_complete()


@pytest.mark.parametrize("required,method,trusted", [
    (False, "model.deepseek.chat", True),
    (True, "provider.yahoo.history", True),
    (True, "model.deepseek.chat", False),
])
def test_diagnostic_is_not_copied_from_unrelated_policy_method_or_exception(
        tmp_path, required, method, trusted):
    execution = context(tmp_path, model_required=required)
    failure = (RequiredModelUnavailable("NETWORK_TIMEOUT") if trusted else OSError("fixture-secret"))
    if not trusted:
        failure.diagnostic = {"content": "fixture-secret"}
    def fail():
        raise failure
    with pytest.raises(type(failure)):
        with execution.step("daily.preflight", {"fixture": "generic error compatibility"}):
            execution.call_io(method, {"request": "keyless"}, fail)
    errors = [json.loads(path.read_text())["value"] for path in (tmp_path / "evidence").glob("*.json")
              if json.loads(path.read_text())["role"] == "output"]
    assert errors == [{"error_type": type(failure).__name__, "message": "External call failed",
                       "outcome_observed": True}]


def test_valid_optional_diagnostic_is_independently_revalidated(tmp_path):
    execution = complete(tmp_path)
    payload = {"error_type": "RequiredModelUnavailable", "message": "External call failed",
               "outcome_observed": True,
               "diagnostic": RequiredModelUnavailable("HTTP_ERROR", http_status=429).diagnostic}
    rewrite_first_io(tmp_path, lambda call: call.update(method="model.deepseek.chat"),
                     payload, use_error=True)
    assert verify_events(tmp_path, execution.run_id, daily_stage_contract())["stage_count"] == 39


@pytest.mark.parametrize("mutate", [
    lambda p: p["diagnostic"].update(body="fixture-secret"),
    lambda p: p["diagnostic"].update(http_status=True),
    lambda p: p["diagnostic"].update(code="fixture-secret"),
    lambda p: p["diagnostic"].update(schema_path="/chains/0", schema_keyword="type"),
    lambda p: p.update(error_type="OSError"),
    lambda p: p.update(diagnostic=None),
])
def test_rehashed_unsafe_model_error_diagnostic_is_rejected(tmp_path, mutate):
    execution = complete(tmp_path)
    payload = {"error_type": "RequiredModelUnavailable", "message": "External call failed",
               "outcome_observed": True,
               "diagnostic": RequiredModelUnavailable("HTTP_ERROR", http_status=401).diagnostic}
    mutate(payload)
    rewrite_first_io(tmp_path, lambda call: call.update(method="model.deepseek.chat"),
                     payload, use_error=True)
    with pytest.raises(EvidenceInvalid, match="I/O error diagnostic"):
        verify_events(tmp_path, execution.run_id, daily_stage_contract())


def test_model_diagnostic_cannot_be_attached_to_an_unrelated_io_method(tmp_path):
    execution = complete(tmp_path)
    payload = {"error_type": "RequiredModelUnavailable", "message": "External call failed",
               "outcome_observed": True,
               "diagnostic": RequiredModelUnavailable("NETWORK_TIMEOUT").diagnostic}
    rewrite_first_io(tmp_path, lambda _call: None, payload, use_error=True)
    with pytest.raises(EvidenceInvalid, match="I/O error diagnostic"):
        verify_events(tmp_path, execution.run_id, daily_stage_contract())


@pytest.mark.parametrize("relative", [
    "../escape.json", "/absolute.json", "a/../b", "a//b", "a/./b", "a\\b", "\0",
])
def test_evidence_path_traversal_is_rejected(tmp_path, relative):
    with pytest.raises(EvidenceInvalid):
        read_bytes(tmp_path, relative)


def test_symlink_hardlink_parent_and_replacement_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    root = tmp_path / "run"
    root.mkdir()
    (root / "symlink").symlink_to(outside)
    os.link(outside, root / "hardlink")
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    for name in ("symlink", "hardlink", "parent/outside"):
        with pytest.raises(EvidenceInvalid):
            read_bytes(root, name)
    with pytest.raises(EvidenceInvalid):
        atomic_bytes(root, "symlink", b"replace")
    assert outside.read_bytes() == b"secret"


def test_immutable_write_and_schema_binding(tmp_path):
    atomic_json(tmp_path, "a.json", {"v": 1}, exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_json(tmp_path, "a.json", {"v": 2}, exclusive=True)
    ref = file_ref(tmp_path, "a.json", "sample.v1")
    assert verify_ref(tmp_path, ref, schema="sample.v1") == b'{"v":1}\n'
    with pytest.raises(EvidenceInvalid):
        verify_ref(tmp_path, ref, schema="sample.v2")
    assert read_json(tmp_path, "a.json") == {"v": 1}
