"""Worker-owned resources and isolated one-stage execution evidence."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from threading import get_ident

from ..agent_evidence import (EvidenceStore, checked_root, file_ref, read_bytes,
                              read_snapshot, strict_json, verify_events, verify_ref)
from ..redline.execution import (ExecutionContext, REQUIRED_MODEL_STAGES, RuntimePaths, daily_stage_specs)
from .contracts import WorkUnitError, detached, value_hash

CHILD_SEAL_SCHEMA = "tiger.work-unit-child-seal/v1"


class UnitContext:
    def __init__(self, store, lease, *, client_factory=None, register_client=None):
        self.store, self.lease = store, lease
        self._owner = get_ident()
        self._factory, self._register_client = client_factory, register_client
        self._client, self._client_created = None, False
        self.inputs = detached(store.unit(lease.unit_id)["spec"]["inputs"])
        self.check_active()

    def check_active(self):
        if get_ident() != self._owner:
            raise WorkUnitError("CONTEXT_OWNER_MISMATCH", "Only this unit worker may use its context")
        self.store.check_lease(self.lease)

    @property
    def client(self):
        self.check_active()
        if not self._client_created:
            if not callable(self._factory):
                raise WorkUnitError("CLIENT_FACTORY_REQUIRED", "This unit requires its own client factory")
            value = self._factory(self)
            if value is None:
                raise WorkUnitError("CLIENT_FACTORY_INVALID", "A unit client factory returned no client")
            if self._register_client is not None:
                self._register_client(value)
            self._client, self._client_created = value, True
        return self._client

    def heartbeat(self, *, lease_seconds=60):
        self.check_active()
        self.lease = self.store.heartbeat(self.lease, lease_seconds=lease_seconds)
        return self.lease

    def external(self, request, fn, *, operation, ordinal=1, reservation=None, usage=None):
        """Persist before an effect; replay a known native value without calling fn."""
        self.check_active()
        attempt = self.store.begin_attempt(self.lease, request, operation=operation,
                                          ordinal=ordinal, reservation=reservation)
        if attempt.reused:
            return self.store.read_attempt_response(attempt.attempt_id)
        self.store.mark_dispatch(self.lease, attempt.attempt_id)
        # An exception deliberately leaves the dispatch and its reservation
        # unknown. A caller may append a native failure observation separately.
        response = fn()
        amount = usage(response) if callable(usage) else usage
        self.store.record_response(self.lease, attempt.attempt_id, response, usage=amount)
        if amount is not None:
            self.store.settle_attempt(attempt.attempt_id)
        return response

    def publish_result(self, value, *, validator, dispositions=None):
        self.check_active()
        return self.store.publish_result(self.lease, value, validator=validator, dispositions=dispositions)


class UnitExecutionContext(ExecutionContext):
    """New contract only: inherit old owner fences; replace no old stage stream.

    The parent remains on its coordinator thread. A child must be constructed
    inside its worker, and every model send is preceded by a durable intent.
    A recovered settled native response is exposed for trusted reconstruction;
    it never authorizes silently sending that request again.
    """
    def __init__(self, parent, *, work_store, lease, stage, child_root=None):
        if (not isinstance(parent, ExecutionContext) or stage not in REQUIRED_MODEL_STAGES
                or not parent.response_observation_contract
                or parent.run_id != work_store.manifest["run_id"]):
            raise WorkUnitError("CHILD_CONTRACT_INVALID", "Child requires the admitted parent model contract")
        work_store.check_lease(lease)
        specs = [spec for spec in parent._specs if spec.name == stage]
        if len(specs) != 1:
            raise WorkUnitError("CHILD_CONTRACT_INVALID", "Child stage is outside the parent contract")
        expected_root = work_store.root / "children" / lease.unit_id / ("epoch-" + str(lease.epoch))
        root = expected_root if child_root is None else Path(child_root)
        if root != expected_root:
            raise WorkUnitError("CHILD_CONTRACT_INVALID", "Child evidence requires its isolated unit epoch directory")
        checked_root(root, create=True)
        work = checked_root(root / "work", create=True)
        path_names = {name: work / Path(getattr(parent.paths, name)).relative_to(parent.paths.work_dir)
                      for name in parent.paths.__dataclass_fields__ if name != "work_dir"}
        paths = RuntimePaths(work_dir=work, **path_names)
        super().__init__(run_id=parent.run_id, run_date=parent.run_date, paths=paths,
                         policy=parent.policy, store=EvidenceStore(root, parent.run_id),
                         admitted_at=parent.admitted_at, authoritative_paper=parent.authoritative_paper,
                         account_version_before=parent.account_version_before,
                         response_observation_contract=parent.response_observation_contract)
        self._specs = tuple(specs)
        self.work_store, self.lease, self.child_root = work_store, lease, root
        self._task_attempt = None
        self._task_request = None

    def _check_thread(self):
        super()._check_thread()
        self.work_store.check_lease(self.lease)

    def call_io(self, method, inputs, fn):
        if method != "model.deepseek.chat":
            raise WorkUnitError("CHILD_IO_FORBIDDEN", "A model child permits only its admitted model method")
        if self._task_attempt is not None:
            raise WorkUnitError("CHILD_IO_OVERLAP", "Child model calls cannot overlap")
        self._task_request = detached(inputs)
        attempt = self.work_store.begin_attempt(self.lease, self._task_request,
            operation=method, ordinal=1, reservation={"output_tokens": inputs["output_ceiling"]})
        if attempt.reused:
            self._task_request = None
            raise WorkUnitError("NATIVE_REPLAY_REQUIRED", "Settled native reply requires trusted reconstruction")
        self._task_attempt = attempt
        self.work_store.mark_dispatch(self.lease, attempt.attempt_id)
        try:
            return super().call_io(method, inputs, fn)
        finally:
            self._task_attempt, self._task_request = None, None

    def model_attempt_snapshot(self, value):
        # A stale worker may append its actual native reply, while its fenced
        # stage stream and result publication remain closed.
        if get_ident() != self._owner_thread:
            raise WorkUnitError("CONTEXT_OWNER_MISMATCH", "Only this child worker may observe its native reply")
        if self._task_attempt is None or value.get("ordinal") != self._task_attempt.ordinal:
            raise WorkUnitError("ATTEMPT_MISMATCH", "Native reply ordinal differs from durable dispatch")
        from ..llm.json_observation import validate_attempt
        value = validate_attempt(value, self._task_request)
        native = value.get("response")
        amount = None if native is None else {key: native["usage"][key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        self.work_store.record_response(self.lease, self._task_attempt.attempt_id, value, usage=amount)
        if amount is not None:
            if not self.work_store.attempt(self._task_attempt.attempt_id)["state"] == "dispatched":
                self.work_store.settle_attempt(self._task_attempt.attempt_id)
        return super().model_attempt_snapshot(value)

    def claim_model_format_retry(self):
        self._check_thread()
        if self._task_attempt is None or self._task_attempt.ordinal != 1:
            return False
        from ..llm.json_observation import replay_attempt
        admitted = self.work_store.claim_format_retry(self.lease, self._task_attempt.attempt_id,
            eligibility_verifier=lambda request, native: replay_attempt(native, request)["retry_eligible"])
        if admitted:
            next_attempt = self.work_store.begin_attempt(self.lease, self._task_request,
                operation="model.deepseek.chat", ordinal=2,
                reservation={"output_tokens": self._task_request["output_ceiling"]})
            self.work_store.mark_dispatch(self.lease, next_attempt.attempt_id)
            self._task_attempt = next_attempt
        return admitted

    def seal(self):
        self.assert_complete()
        spec = asdict(self._specs[0])
        proof = verify_events(self.child_root, self.run_id, [spec])
        unit = self.work_store.unit(self.lease.unit_id)
        record = self.summary()[0]
        value = {"schema": CHILD_SEAL_SCHEMA, "manifest_sha256": self.work_store.manifest_sha256,
                 "tenant_id": self.lease.tenant_id, "run_id": self.run_id, "unit_id": self.lease.unit_id,
                 "epoch": self.lease.epoch, "source_sha256": self.work_store.manifest["source_sha256"],
                 "policy_sha256": self.work_store.manifest["policy_sha256"],
                 "input_sha256": self.work_store.manifest["input_sha256"],
                 "unit_input_sha256": value_hash(unit["spec"]["inputs"]),
                 "unit_contract_sha256": value_hash(unit["spec"]["contract"]),
                 "stage": spec, "child_path": str(self.child_root.relative_to(self.work_store.root)),
                 "events_ref": file_ref(self.child_root, "events.jsonl", "tiger.execution-events/v1"),
                 "event_count": proof["event_count"], "last_event_sha256": proof["last_event_sha256"],
                 "input_ref": record["input_ref"], "output_ref": record["output_ref"],
                 "evidence_refs": proof["evidence_refs"]}
        return self.work_store._blob(value, CHILD_SEAL_SCHEMA)


def verify_child_seal(work_store, ref):
    value = work_store.read_artifact(ref, schema=CHILD_SEAL_SCHEMA)
    unit = work_store.unit(value["unit_id"])
    manifest = work_store.manifest
    canonical_spec = next((asdict(spec) for spec in daily_stage_specs(True, authoritative_paper=True, intelligence=True)
                           if spec.name == value["stage"]["name"]), None)
    if (value["schema"] != CHILD_SEAL_SCHEMA or value["manifest_sha256"] != work_store.manifest_sha256
            or value["tenant_id"] != manifest["tenant_id"] or value["run_id"] != manifest["run_id"]
            or any(value[name] != manifest[name] for name in ("source_sha256", "policy_sha256", "input_sha256"))
            or value["unit_input_sha256"] != value_hash(unit["spec"]["inputs"])
            or value["unit_contract_sha256"] != value_hash(unit["spec"]["contract"])
            or value["epoch"] != unit["epoch"] or value["stage"]["name"] not in REQUIRED_MODEL_STAGES
            or value["stage"] != canonical_spec
            or value["child_path"] != "children/" + value["unit_id"] + "/epoch-" + str(value["epoch"])):
        raise WorkUnitError("CHILD_SEAL_INVALID", "Child seal identity differs")
    root = checked_root(work_store.root / value["child_path"])
    verify_ref(root, value["events_ref"], schema="tiger.execution-events/v1")
    proof = verify_events(root, value["run_id"], [value["stage"]])
    if any(value[name] != proof[name] for name in ("event_count", "last_event_sha256", "evidence_refs")):
        raise WorkUnitError("CHILD_SEAL_INVALID", "Child seal evidence differs")
    events = [strict_json(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    if value["input_ref"] != events[0]["input_ref"] or value["output_ref"] != events[-1]["output_ref"]:
        raise WorkUnitError("CHILD_SEAL_INVALID", "Child seal stage references differ")
    output = read_snapshot(root, value["output_ref"], run_id=value["run_id"], stage=value["stage"]["name"], role="output")
    if len(output["io_calls"]) != 1 or output["io_calls"][0]["method"] != "model.deepseek.chat":
        raise WorkUnitError("CHILD_SEAL_INVALID", "Child requires exactly one admitted logical model call")
    call = output["io_calls"][0]
    request = read_snapshot(root, call["input_ref"], run_id=value["run_id"], stage=value["stage"]["name"], role="input")
    call_output = read_snapshot(root, call["output_ref"], run_id=value["run_id"], stage=value["stage"]["name"], role="output")
    with work_store._tx() as connection:
        attempts = sorted((row for row in work_store._rows(connection, "attempts")
                           if row["unit_id"] == value["unit_id"]), key=lambda row: row["ordinal"])
    observations = call_output["response_observation"]["attempts"]
    if len(attempts) != len(observations) or not attempts:
        raise WorkUnitError("CHILD_SEAL_INVALID", "Child model attempt count differs")
    for attempt, observation in zip(attempts, observations):
        native = read_snapshot(root, observation["observation_ref"], run_id=value["run_id"], stage=value["stage"]["name"], role="output")
        if (attempt["state"] != "settled" or work_store.read_attempt_request(attempt["attempt_id"]) != request
                or native != work_store.read_attempt_response(attempt["attempt_id"])):
            raise WorkUnitError("CHILD_SEAL_INVALID", "Child native model receipt binding differs")
    return value
