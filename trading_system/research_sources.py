"""Worker-owned source observations joined by the admitted parent owner.

The acquisition archive owns source attempts and quotas. These children bind
its exact request to the parent evidence stream without sharing that stream
between threads or introducing a second source-attempt ledger.
"""
from __future__ import annotations

from dataclasses import asdict
from threading import Lock

from .agent_evidence import (EvidenceStore, atomic_bytes, atomic_json, checked_root,
    file_ref, read_bytes, read_json, read_snapshot, strict_json, verify_events, verify_ref)
from .intelligence.contracts import SourceError
from .intelligence.parse_limits import ParserResourcePool, trusted_parser_resources
from .redline.execution import ExecutionContext, daily_stage_specs
from .research_work import active_research_work
from .tasks.contracts import detached, value_hash

SOURCE_CHILD = "tiger.source-child-seal/v1"
SOURCE_JOIN = "tiger.source-child-join/v1"
_STAGES = {"search.collect", "clean.llm_semantic"}


def _binding(store):
    return {"manifest_sha256": store.manifest_sha256,
        **{name: store.manifest[name] for name in
           ("run_id", "tenant_id", "source_sha256", "policy_sha256", "input_sha256", "as_of")}}


def verify_source_child(store, reference):
    seal = store.read_artifact(reference, schema=SOURCE_CHILD)
    stage = seal.get("stage", {})
    canonical = next((asdict(spec) for spec in daily_stage_specs(True,
        authoritative_paper=True, intelligence=True) if spec.name == stage.get("name")), None)
    fields = {"schema", "manifest_sha256", "run_id", "tenant_id", "source_sha256", "policy_sha256",
        "input_sha256", "as_of", "stage", "request", "child_path", "events_ref", "event_count",
        "last_event_sha256", "evidence_refs", "input_ref", "output_ref"}
    if (set(seal) != fields or seal["schema"] != SOURCE_CHILD
            or any(seal[name] != value for name, value in _binding(store).items())
            or stage != canonical or stage["name"] not in _STAGES
            or seal["child_path"] != "source-children/" + value_hash(seal["request"])):
        raise ValueError("SOURCE_CHILD_IDENTITY_MISMATCH")
    root = checked_root(store.root / seal["child_path"])
    verify_ref(root, seal["events_ref"], schema="tiger.execution-events/v1")
    proof = verify_events(root, store.manifest["run_id"], [stage])
    if any(seal[name] != proof[name] for name in ("event_count", "last_event_sha256", "evidence_refs")):
        raise ValueError("SOURCE_CHILD_EVENTS_MISMATCH")
    events = [strict_json(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    if seal["input_ref"] != events[0]["input_ref"] or seal["output_ref"] != events[-1]["output_ref"]:
        raise ValueError("SOURCE_CHILD_STAGE_MISMATCH")
    request = read_snapshot(root, seal["input_ref"], run_id=seal["run_id"], stage=stage["name"], role="input")
    output = read_snapshot(root, seal["output_ref"], run_id=seal["run_id"], stage=stage["name"], role="output")
    calls = output["io_calls"]
    if request != seal["request"] or len(calls) != 1 or calls[0]["method"] != "intelligence.source.request":
        raise ValueError("SOURCE_CHILD_REQUEST_MISMATCH")
    call = calls[0]
    observed_input = read_snapshot(root, call["input_ref"], run_id=seal["run_id"], stage=stage["name"], role="input")
    value = output["value"]
    if observed_input != request or value.get("state") != ("failed" if "error_ref" in call else "observed"):
        raise ValueError("SOURCE_CHILD_RESULT_MISMATCH")
    observed_output = read_snapshot(root, call.get("output_ref", call.get("error_ref")),
        run_id=seal["run_id"], stage=stage["name"], role="output")
    if "output_ref" in call and (set(value) != {"state", "response"} or value["response"] != observed_output):
        raise ValueError("SOURCE_CHILD_RESULT_MISMATCH")
    if "error_ref" in call and (set(value) != {"state", "error"} or observed_output["error_type"] != "SourceError"):
        raise ValueError("SOURCE_CHILD_ERROR_MISMATCH")
    return seal


def expand_source_join(parent_root, store, stage, inputs, output):
    expected_input = {"schema": SOURCE_JOIN, **_binding(store),
        "child_seal_ref": output.get("child_seal_ref")}
    if inputs != expected_input or set(output) != set(expected_input) | {"request", "io_calls"}:
        raise ValueError("SOURCE_JOIN_IDENTITY_MISMATCH")
    if any(output[name] != value for name, value in inputs.items()):
        raise ValueError("SOURCE_JOIN_BINDING_MISMATCH")
    seal = verify_source_child(store, inputs["child_seal_ref"])
    if seal["stage"]["name"] != stage or output["request"] != seal["request"]:
        raise ValueError("SOURCE_JOIN_STAGE_MISMATCH")
    root = store.root / seal["child_path"]
    child = read_snapshot(root, seal["output_ref"], run_id=seal["run_id"], stage=stage, role="output")
    if output["io_calls"] != child["io_calls"]:
        raise ValueError("SOURCE_JOIN_CALL_MISMATCH")
    for ref in seal["evidence_refs"]:
        if verify_ref(parent_root, ref) != verify_ref(root, ref):
            raise ValueError("SOURCE_JOIN_BYTES_MISMATCH")
    return detached(child["io_calls"][0])


class SourceObservations:
    def __init__(self, parent):
        if not active_research_work(parent):
            raise ValueError("SOURCE_WORK_NOT_ADMITTED")
        self.parent, self.store = parent, parent.research_work_store
        self._lock = Lock()
        self._joined = set()
        policy = parent.research_work_policy
        self.pool = ParserResourcePool(max_workers=policy["parser_workers"],
            memory_bytes=policy["max_active_memory_bytes"], temporary_bytes=policy["parser_temporary_bytes"],
            wait_for_capacity=True)

    def observer(self, source_id):
        def observe(method, inputs, invoke):
            if (method != "intelligence.source.request" or inputs.get("source_id") != source_id
                    or inputs.get("schema") != "tiger.intelligence-source-request/v1"):
                raise ValueError("SOURCE_WORK_REQUEST_MISMATCH")
            active = self.parent._active
            if active is None or active.spec.name not in _STAGES:
                raise ValueError("SOURCE_WORK_STAGE_MISMATCH")
            relative = "source-children/" + value_hash(inputs)
            root = checked_root(self.store.root / relative, create=True)
            # Archive reservations serialize identical source attempts. This
            # extra fence also makes direct reuse safe before returning to it.
            with self._lock:
                if (root / "seal-ref.json").exists():
                    reference = read_json(root, "seal-ref.json")
                    seal = verify_source_child(self.store, reference)
                    if seal["stage"] != asdict(active.spec):
                        raise ValueError("SOURCE_WORK_STAGE_CHANGED")
                    result = read_snapshot(root, seal["output_ref"], run_id=self.parent.run_id,
                        stage=active.spec.name, role="output")["value"]
                    if result["state"] == "observed":
                        return result["response"]
                    raise _source_error(result["error"])
                if (root / "events.jsonl").exists():
                    raise ValueError("SOURCE_WORK_RECONCILIATION_REQUIRED")
                child = ExecutionContext(run_id=self.parent.run_id, run_date=self.parent.run_date,
                    paths=self.parent.paths, policy=self.parent.policy, store=EvidenceStore(root, self.parent.run_id),
                    admitted_at=self.parent.admitted_at, authoritative_paper=self.parent.authoritative_paper,
                    account_version_before=self.parent.account_version_before)
                child._specs = (active.spec,)
                # Publish intent while holding the shared local fence. The
                # child owner remains this worker; no parent calls occur here.
                atomic_json(root, "request.json", inputs, exclusive=True)
            error = None
            with child.step(active.spec.name, inputs) as scope:
                try:
                    with trusted_parser_resources(self.pool):
                        response = child.call_io(method, inputs, invoke)
                    result = {"state": "observed", "response": response}
                except SourceError as caught:
                    error = caught
                    result = {"state": "failed", "error": caught.to_dict()}
                scope.output(result)
            child.assert_complete()
            proof = verify_events(root, child.run_id, [asdict(active.spec)])
            record = child.summary()[0]
            seal = {"schema": SOURCE_CHILD, **_binding(self.store), "stage": asdict(active.spec),
                "request": detached(inputs), "child_path": relative,
                "events_ref": file_ref(root, "events.jsonl", "tiger.execution-events/v1"),
                **{name: proof[name] for name in ("event_count", "last_event_sha256", "evidence_refs")},
                "input_ref": record["input_ref"], "output_ref": record["output_ref"]}
            reference = self.store._blob(seal, SOURCE_CHILD)
            verify_source_child(self.store, reference)
            atomic_json(root, "seal-ref.json", reference, exclusive=True)
            if error is not None:
                raise error
            return response
        return observe

    def drain(self):
        self.parent._check_thread()
        active = self.parent._active
        if active is None:
            raise ValueError("SOURCE_JOIN_STAGE_REQUIRED")
        directory = self.store.root / "source-children"
        if not directory.exists():
            return
        for path in sorted(directory.iterdir()):
            root = checked_root(path)
            if not (root / "seal-ref.json").exists():
                continue  # Archive unknown remains a hard reconciliation gate.
            reference = read_json(root, "seal-ref.json")
            if reference["sha256"] in self._joined:
                continue
            seal = verify_source_child(self.store, reference)
            if seal["stage"]["name"] != active.spec.name:
                continue
            for ref in seal["evidence_refs"]:
                raw = verify_ref(root, ref)
                try:
                    atomic_bytes(self.parent.store.root, ref["path"], raw, exclusive=True)
                except FileExistsError:
                    if read_bytes(self.parent.store.root, ref["path"]) != raw:
                        raise ValueError("SOURCE_JOIN_EVIDENCE_COLLISION") from None
            inputs = {"schema": SOURCE_JOIN, **_binding(self.store), "child_seal_ref": reference}
            child = read_snapshot(root, seal["output_ref"], run_id=seal["run_id"],
                stage=active.spec.name, role="output")
            output = {**inputs, "request": seal["request"], "io_calls": child["io_calls"]}
            expand_source_join(self.parent.store.root, self.store, active.spec.name, inputs, output)
            self.parent.call_io("tasks.source.join", inputs, lambda: output)
            self._joined.add(reference["sha256"])


def _source_error(value):
    details = value.get("details", {})
    return SourceError(value["code"], delivery=details.get("delivery", "unknown"),
        http_status=details.get("http_status"), retry_after=details.get("retry_after"),
        bytes_received=details.get("bytes_received", 0), details=details)
