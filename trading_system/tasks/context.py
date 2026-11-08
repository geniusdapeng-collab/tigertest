"""Worker-owned resources and isolated one-stage execution evidence."""
from __future__ import annotations

from dataclasses import asdict
import re
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
        saved = self.work_store.attempts_for_unit(self.lease.unit_id)
        if saved:
            original = self.work_store.read_attempt_request(saved[0]["attempt_id"])
            if ({key: value for key, value in original.items() if key != "call_id"}
                    != {key: value for key, value in self._task_request.items() if key != "call_id"}):
                self._task_request = None
                raise WorkUnitError("ATTEMPT_MISMATCH", "Recovered model request differs from its original wire or schema")
            if saved[0]["state"] == "settled":
                self._task_request = original
                try:
                    return super().call_io(method, original, lambda: self._replay_native_call(original, saved))
                finally:
                    self._task_request = None
        attempt = self.work_store.begin_attempt(self.lease, self._task_request,
            operation=method, ordinal=1, reservation={"output_tokens": inputs["output_ceiling"]})
        if attempt.reused:
            self._task_request = None
            raise WorkUnitError("NATIVE_REPLAY_REQUIRED", "Settled native reply requires trusted reconstruction")
        self._task_attempt = attempt
        try:
            return super().call_io(method, inputs, fn)
        finally:
            self._task_attempt, self._task_request = None, None

    def model_call_id(self, proposed):
        """Give a reconstructed client the original local call identity.

        The provider wire is still compared in full by call_io. This hook
        permits a known-not-sent intent to resume its first actual dispatch
        without rewriting either its intent or any native observation.
        """
        self._check_thread()
        saved = self.work_store.attempts_for_unit(self.lease.unit_id)
        result = (self.work_store.read_attempt_request(saved[0]["attempt_id"])["call_id"]
                  if saved else proposed)
        if type(result) is not str or re.fullmatch(r"[0-9a-f]{32}", result) is None:
            raise WorkUnitError("CHILD_CONTRACT_INVALID", "Invalid recovered model call identity")
        return result

    def model_transport_dispatch(self, request):
        """Mark the actual provider boundary after local budget validation."""
        self._check_thread()
        if (self._task_attempt is None or self._task_request is None
                or detached(request) != self._task_request.get("request")):
            raise WorkUnitError("ATTEMPT_MISMATCH", "Transport wire differs from its durable model intent")
        scope = self._active
        if scope is None or len([handle for handle in scope._handles
                if not handle.completed and handle.method == "model.deepseek.chat"]) != 1:
            raise WorkUnitError("CHILD_IO_FORBIDDEN", "Transport requires its active unfinished model IO")
        self.work_store.mark_dispatch(self.lease, self._task_attempt.attempt_id)

    def _replay_native_call(self, inputs, attempts):
        """Derive the standard call proof from original settled native values.

        A new epoch writes its own snapshots of the unchanged observations.
        It neither invokes the supplied transport closure nor consumes retry
        quota, including when the original complete reply remains rejected.
        """
        from ..llm.deepseek_required import MODEL, SEARCH_MODE, _canonical
        from ..llm.json_observation import (contract_schemas, digest as model_digest,
                                            replay_attempt, validate_reference_observation)
        from ..redline.model_diagnostics import RequiredModelUnavailable
        observations, last_native, last_verdict, previous_verdict = [], None, None, None
        for ordinal, attempt in enumerate(attempts, 1):
            if attempt["ordinal"] != ordinal or attempt["state"] != "settled":
                raise WorkUnitError("ATTEMPT_UNKNOWN", "Native replay requires every original attempt to be settled")
            if ordinal == 2 and (previous_verdict is None or not previous_verdict["retry_eligible"]):
                raise WorkUnitError("NATIVE_REPLAY_INVALID", "Saved reanswer lacks original replay eligibility")
            native = self.work_store.read_attempt_response(attempt["attempt_id"])
            try:
                verdict = replay_attempt(native, inputs)
            except ValueError:
                raise WorkUnitError("NATIVE_REPLAY_INVALID", "Saved native reply failed its original contract") from None
            ref = self._snapshot(inputs["stage"], "output", native)
            observations.append({"ordinal": ordinal, "observation_ref": ref, "diagnostic": verdict["diagnostic"]})
            last_native, last_verdict, previous_verdict = native, verdict, verdict
        if last_native is None or last_verdict is None:
            raise WorkUnitError("NATIVE_REPLAY_INVALID", "Native replay has no original reply")
        observation = validate_reference_observation({"schema": inputs["response_observation_contract"],
            "call_id": inputs["call_id"], "request_sha256": inputs["request_sha256"],
            "attempts": observations, "retry_used": len(observations) == 2})
        diagnostic = last_verdict["diagnostic"]
        if diagnostic is not None:
            fields = {name: diagnostic[name] for name in (
                "http_status", "schema_path", "schema_keyword", "json_stage", "transport") if name in diagnostic}
            raise RequiredModelUnavailable(diagnostic["code"], **fields, observation=observation)
        response = detached(last_native["response"])
        tool = "clean_protocol" in inputs
        if not tool:
            response["choices"][0]["message"]["content"] = _canonical(last_verdict["parsed"]).decode("utf-8")
        result = {"schema": contract_schemas(inputs["response_observation_contract"])["call"],
            "response_observation_contract": inputs["response_observation_contract"],
            "call_id": inputs["call_id"], "stage": inputs["stage"], "provider": "deepseek",
            "requested_model": MODEL, "response_id": response["id"], "model": MODEL,
            "release": inputs["release"], "usage": last_verdict["usage"], "scene": inputs["scene"],
            "thinking_mode": inputs["thinking_mode"], "reasoning_effort": inputs["reasoning_effort"],
            "input_token_estimate": inputs["input_token_estimate"], "output_ceiling": inputs["output_ceiling"],
            "policy_version": inputs["policy_version"], "registry_version": inputs["registry_version"],
            "tokenizer_sha256": inputs["tokenizer_sha256"], "finish_reason": "tool_calls" if tool else "stop",
            "request_sha256": inputs["request_sha256"], "response_sha256": model_digest(response),
            "transport_response_sha256": last_native["transport"]["body_sha256"],
            "schema_sha256": inputs["schema_sha256"], "response": response,
            "system_fingerprint": response["system_fingerprint"], "search_mode": SEARCH_MODE,
            "parsed": last_verdict["parsed"], "response_observation": observation}
        if tool:
            result.update(clean_protocol=detached(inputs["clean_protocol"]), native_parsed=last_verdict["native_parsed"])
        return result

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
        state = self.work_store.attempt(self._task_attempt.attempt_id)["state"]
        if state == "prepared":
            if native is not None:
                raise WorkUnitError("DISPATCH_MISSING", "A native provider reply requires its actual dispatch boundary")
            # A local budget/configuration rejection may have a closed native
            # failure diagnostic, but no provider send or billable response.
            return super().model_attempt_snapshot(value)
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
