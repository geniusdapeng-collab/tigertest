"""Independent replay of explicitly admitted research work and child joins.

A validated SQLite row or a child success flag is insufficient. This verifier
binds the ledger to the parent admission, reconstructs each legal unit and
request, replays its original native observations, and recomputes normalized
facts, document review, source qualification and the common research snapshot.
Historical intelligence evidence never enters this module implicitly.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from functools import wraps
import re

from ..agent_evidence import (EvidenceInvalid, digest, json_bytes, normalize,
    read_bytes, read_json, read_snapshot, verify_ref)
from ..redline import LLMUnavailable
from ..research_work import (RESEARCH_WORK_CONTRACT, SNAPSHOT_CONTRACT,
    WORK_CLEAN_CONTRACT, validate_research_work_policy, work_manifest)
from ..search.models import RawDocument
from ..tasks.contracts import UnitSpec, value_hash
from ..tasks.context import verify_child_seal
from ..tasks.store import WorkStore
from . import intelligence_required as legacy
from .deepseek_required import (MODEL, RELEASE, SEARCH_MODE, sanitize_model_text,
    sanitize_model_value, validate_schema)
from .json_observation import TOOL_CONTRACT, contract_schemas
from .model_budget import (COUNTER_ID, TokenBudgetError, chat_request_core,
                           frozen_budget_contract, plan_request)
from .scene_policy import KERNEL_SCENE_MAP, ScenePolicyError, scene_config
from .work_semantics import (annotation_payload, digest as semantic_hash, normalize_annotation,
    merge_segment_annotations, namespace_segment_annotation, task_evidence_view,
    inspect_document_review, text_digest, validate_annotation, validate_raw_documents)

JOIN_SCHEMA = "tiger.work-child-join/v1"
SUMMARY_SCHEMA = "tiger.research-work-proof/v1"
ACCOUNTING_SCHEMA = "tiger.work-native-accounting/v1"


def _fail(stage=None):
    raise EvidenceInvalid("MODEL_REQUIRED_FAILED",
        "Frozen research-work evidence is missing or differs", stage=stage)


def _same(actual, expected, stage=None):
    if json_bytes(normalize(actual)) != json_bytes(normalize(expected)):
        _fail(stage)


def _boundary(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except EvidenceInvalid:
            raise
        except (KeyError, TypeError, ValueError, IndexError, AttributeError,
                OSError, OverflowError, RecursionError, TokenBudgetError,
                ScenePolicyError, LLMUnavailable) as error:
            raise EvidenceInvalid("MODEL_REQUIRED_FAILED",
                "Frozen research-work evidence is missing or differs") from error
    return checked


def _document_unit(raw, phase, *, assessment, validation_context):
    extract_id = "doc-" + semantic_hash(asdict(raw))[:32] + ":extract"
    identity = "doc-" + semantic_hash(asdict(raw))[:32] + ":" + phase
    return UnitSpec(identity, "document." + phase,
        {"raw": asdict(raw), "target_ids": list(legacy.TARGET_IDS),
         "assessment": assessment, "validation_context": validation_context},
        {"schema": WORK_CLEAN_CONTRACT, "validator": "document-" + phase + "/v1",
         "phase": phase, "stage": "clean.llm_semantic"},
        dependencies=() if phase == "extract" else (extract_id,),
        document_ids=(raw.doc_id,), terminal_documents=(raw.doc_id,) if phase == "document_review" else ())


class ResearchWorkProof:
    @_boundary
    def __init__(self, replay):
        self.replay = replay
        self.policy = validate_research_work_policy(replay.policy["research_work"])
        counter = frozen_budget_contract(read_bytes(replay.run, "source/trading_system/llm/model_budget.py"))
        _same(replay.budget_contract, counter)
        if (replay.run.name != replay.admission["run_id"]
                or replay.observation_contract != TOOL_CONTRACT
                or counter["counter_id"] != COUNTER_ID
                or read_json(replay.run, "policy.json") != replay.policy
                or digest(json_bytes(replay.policy)) != replay.admission["policy_sha256"]):
            _fail()
        self.store = WorkStore.open(replay.run / "tasks", work_manifest(replay.admission))
        self.verified_units = set()
        self.document_hashes = {}
        self.work_call_ids = set()
        self.snapshot = self.snapshot_reference = None

    def plan(self, stage, arguments):
        """Rebuild the entire supported wire without a client or transport."""
        if (set(arguments) - {"system", "user", "schema_hint", "schema", "public_payload", "temperature"}
                or not {"system", "user", "schema_hint", "schema"} <= set(arguments)):
            _fail(stage)
        thinking = scene_config(KERNEL_SCENE_MAP[stage], self.replay.scene_policy_path)["thinking"]
        user = sanitize_model_text(arguments["user"])
        if arguments.get("public_payload") is not None:
            user += "\n" + json_bytes(sanitize_model_value(arguments["public_payload"])).decode("utf-8").rstrip("\n")
        schema = arguments["schema"]
        _same(sanitize_model_value(schema), schema, stage)
        return plan_request(chat_request_core(system=sanitize_model_text(arguments["system"]),
            user=user, schema=schema, schema_hint=sanitize_model_value(arguments["schema_hint"]),
            thinking_mode=thinking["type"], effort=thinking.get("effort"),
            temperature=arguments.get("temperature", 0.2)),
            counter_id=self.replay.budget_contract["counter_id"])

    @_boundary
    def verify_chat(self, stage, call, arguments):
        replay = self.replay
        if (not isinstance(call, dict) or set(call) != {"index", "method", "input_ref", "output_ref"}
                or type(call["index"]) is not int or call["index"] < 1
                or call["method"] != "model.deepseek.chat"):
            _fail(stage)
        request = read_snapshot(replay.run, call["input_ref"], run_id=replay.run.name, stage=stage, role="input")
        proof = read_snapshot(replay.run, call["output_ref"], run_id=replay.run.name, stage=stage, role="output")
        call_id = request["call_id"]
        if type(call_id) is not str or re.fullmatch(r"[0-9a-f]{32}", call_id) is None or call_id in replay.call_ids:
            _fail(stage)
        body, count, ceiling = self.plan(stage, arguments)
        thinking = body["thinking"]
        metadata = {"scene": KERNEL_SCENE_MAP[stage], "thinking_mode": thinking["type"],
            "reasoning_effort": body.get("reasoning_effort"), "input_token_estimate": count,
            "output_ceiling": ceiling, **{key: replay.budget_contract[key] for key in
                ("policy_version", "registry_version", "tokenizer_sha256")}}
        schemas = contract_schemas(replay.observation_contract)
        expected = {"schema": schemas["request"], "response_observation_contract": replay.observation_contract,
            "call_id": call_id, "stage": stage, "provider": "deepseek", "method": "POST",
            "endpoint": "https://api.deepseek.com/chat/completions", "request": body,
            "request_sha256": semantic_hash(body), "output_schema": arguments["schema"],
            "schema_sha256": semantic_hash(arguments["schema"]), "release": RELEASE, **metadata}
        expected["rejected_native_contract"] = self.policy["rejected_native_contract"]
        _same(request, expected, stage)
        fields = {"schema", "response_observation_contract", "call_id", "stage", "provider",
            "requested_model", "response_id", "model", "release", "usage", "scene", "thinking_mode",
            "reasoning_effort", "input_token_estimate", "output_ceiling", "policy_version",
            "registry_version", "tokenizer_sha256", "finish_reason", "request_sha256",
            "response_sha256", "transport_response_sha256", "schema_sha256", "response",
            "system_fingerprint", "search_mode", "parsed", "response_observation"}
        if (not isinstance(proof, dict) or set(proof) != fields
                or proof["schema"] != schemas["call"]
                or proof["response_observation_contract"] != replay.observation_contract
                or proof["call_id"] != call_id or proof["stage"] != stage or proof["provider"] != "deepseek"
                or proof["requested_model"] != MODEL or proof["model"] != MODEL or proof["release"] != RELEASE
                or proof["finish_reason"] != "stop" or proof["search_mode"] != SEARCH_MODE
                or proof["request_sha256"] != expected["request_sha256"]
                or proof["schema_sha256"] != expected["schema_sha256"]
                or any(proof[key] != value for key, value in metadata.items())):
            _fail(stage)
        replay.call_ids.add(call_id)
        replay._chat_observation(stage, request, proof)
        validate_schema(proof["parsed"], arguments["schema"])
        replay.response_ids.append(proof["response_id"])
        replay.counts[stage] += 1
        self.work_call_ids.add(call_id)
        return deepcopy(proof["parsed"])

    @_boundary
    def verify_join(self, stage, call, expected_spec, arguments, *, stage_input):
        replay, store = self.replay, self.store
        spec = expected_spec.to_dict()
        unit_id = spec["unit_id"]
        if unit_id in self.verified_units:
            _fail(stage)
        if (not isinstance(call, dict) or set(call) != {"index", "method", "input_ref", "output_ref"}
                or type(call["index"]) is not int or call["index"] < 1 or call["method"] != "tasks.child.join"):
            _fail(stage)
        unit = store.unit(unit_id)
        _same(unit["spec"], spec, stage)
        result = store.read_result(unit_id)
        saved = result["value"]
        if not isinstance(saved, dict) or set(saved) != {"child_seal_ref", "output"}:
            _fail(stage)
        request = read_snapshot(replay.run, call["input_ref"], run_id=replay.run.name, stage=stage, role="input")
        _same(request, {"contract": RESEARCH_WORK_CONTRACT, "unit_id": unit_id,
            "manifest_sha256": store.manifest_sha256, "result_ref": unit["result_ref"]}, stage)
        joined = read_snapshot(replay.run, call["output_ref"], run_id=replay.run.name, stage=stage, role="output")
        seal = verify_child_seal(store, saved["child_seal_ref"])
        if seal["unit_id"] != unit_id or seal["stage"]["name"] != stage:
            _fail(stage)
        child_root = store.root / seal["child_path"]
        child_input = read_snapshot(child_root, seal["input_ref"], run_id=replay.run.name,
                                   stage=stage, role="input")
        _same(child_input, stage_input, stage)
        output = read_snapshot(child_root, seal["output_ref"], run_id=replay.run.name,
                               stage=stage, role="output")
        _same(output["value"], saved["output"], stage)
        _same(joined, {"schema": JOIN_SCHEMA, "unit_id": unit_id,
            "manifest_sha256": store.manifest_sha256, "child_seal_ref": saved["child_seal_ref"],
            "result_ref": unit["result_ref"], "io_calls": output["io_calls"], "value": saved["output"]}, stage)
        if len(output["io_calls"]) != 1:
            _fail(stage)
        model_call = output["io_calls"][0]
        model_proof = read_snapshot(child_root, model_call["output_ref"], run_id=replay.run.name,
                                    stage=stage, role="output")
        refs = [*seal["evidence_refs"], *(row["observation_ref"]
            for row in model_proof["response_observation"]["attempts"])]
        for ref in refs:
            if verify_ref(replay.run, ref) != verify_ref(child_root, ref):
                _fail(stage)
        native = self.verify_chat(stage, model_call, arguments)
        self.verified_units.add(unit_id)
        return deepcopy(saved["output"]), native, result

    @_boundary
    def verify_document(self, raw, extract_call_or_calls, review_call=None, *,
                        validation_context, return_count=False):
        from .work_required import (document_arguments, document_unit_specs,
            merged_annotation_limits, plan_document_segments,
            segment_summary_arguments, summary_schema)

        class Planner:
            def __init__(self, verifier):
                self.verifier = verifier

            def plan_chat(self, **arguments):
                return self.verifier.plan("clean.llm_semantic", arguments)

        validate_raw_documents([raw])
        calls = (extract_call_or_calls if review_call is None
                 else [extract_call_or_calls, review_call])
        stage = "clean.llm_semantic"
        if raw.doc_id in self.document_hashes:
            _fail(stage)
        plan = (plan_document_segments(Planner(self), raw, legacy.TARGET_IDS,
            self.replay.assessment, self.policy)
            if "segment_input_token_limit" in self.policy else None)
        specs, units, _entries = document_unit_specs(raw, plan, legacy.TARGET_IDS,
            self.replay.assessment, validation_context, self.policy)
        by_id = {spec.unit_id: spec for spec in specs}
        if len(calls) < len(specs):
            _fail(stage)
        calls = calls[:len(specs)]
        if plan is None:
            extract_spec = by_id[units["extracts"][0]]
            args = document_arguments("extract", raw, legacy.TARGET_IDS, self.replay.assessment)
            value, native, result = self.verify_join(stage, calls[0], extract_spec, args,
                stage_input={"contract": WORK_CLEAN_CONTRACT, "phase": "extract",
                             "unit_id": extract_spec.unit_id, "arguments": args})
            document = normalize_annotation(raw, native["annotation"], legacy.TARGET_IDS)
            validation = validate_annotation(document, self.replay.cutoff, **validation_context)
            _same(value, {"phase": "extract", "document_id": raw.doc_id, "segment_id": None,
                "native": native, "annotation": annotation_payload(document),
                "validation": validation}, stage)
            _same(result["dispositions"], {}, stage)
            review_spec = by_id[units["reviews"][0]]
            args = document_arguments("document_review", raw, legacy.TARGET_IDS,
                self.replay.assessment, annotation=annotation_payload(document))
            value, native, result = self.verify_join(stage, calls[1], review_spec, args,
                stage_input={"contract": WORK_CLEAN_CONTRACT, "phase": "document_review",
                             "unit_id": review_spec.unit_id, "arguments": args})
            review = inspect_document_review(document, native)
            _same(value, {"phase": "document_review", "document_id": raw.doc_id,
                "segment_id": None, "native": native,
                "annotation": annotation_payload(document), "validation": validation,
                "review": review}, stage)
            review_accepted = review["review_accepted"]
        else:
            manifest, segments = plan
            annotations, review_receipts, index = [], [], 0
            review_accepted = True
            for number, segment in enumerate(segments, 1):
                extract_spec = by_id[units["extracts"][number - 1]]
                args = document_arguments("extract", segment, legacy.TARGET_IDS,
                                         self.replay.assessment)
                value, native, result = self.verify_join(stage, calls[index], extract_spec, args,
                    stage_input={"contract": WORK_CLEAN_CONTRACT, "phase": "extract",
                                 "unit_id": extract_spec.unit_id, "arguments": args})
                local = normalize_annotation(segment, native["annotation"], legacy.TARGET_IDS)
                validation = validate_annotation(local, self.replay.cutoff, **validation_context)
                segment_id = manifest["segments"][number - 1]["id"]
                _same(value, {"phase": "extract", "document_id": raw.doc_id,
                    "segment_id": segment_id, "native": native,
                    "annotation": annotation_payload(local), "validation": validation}, stage)
                _same(result["dispositions"], {}, stage)
                index += 1
                review_spec = by_id[units["reviews"][number - 1]]
                args = document_arguments("document_review", segment, legacy.TARGET_IDS,
                    self.replay.assessment, annotation=annotation_payload(local))
                value, native, result = self.verify_join(stage, calls[index], review_spec, args,
                    stage_input={"contract": WORK_CLEAN_CONTRACT,
                                 "phase": "document_review",
                                 "unit_id": review_spec.unit_id, "arguments": args})
                review = inspect_document_review(local, native)
                _same(value, {"phase": "document_review", "document_id": raw.doc_id,
                    "segment_id": segment_id, "native": native,
                    "annotation": annotation_payload(local), "validation": validation,
                    "review": review}, stage)
                _same(result["dispositions"], {}, stage)
                review_accepted = review_accepted and review["review_accepted"]
                review_receipts.append(review)
                annotations.append(namespace_segment_annotation(
                    annotation_payload(local), number,
                    manifest["segments"][number - 1]["start"]))
                index += 1
            summary_spec = by_id[units["summary"]]
            segment_rows = [{"segment_id": manifest["segments"][number]["id"],
                             "summary_zh": row["summary_zh"]}
                            for number, row in enumerate(annotations)]
            args = segment_summary_arguments(raw, segment_rows, self.replay.assessment)
            value, native, result = self.verify_join(stage, calls[index], summary_spec, args,
                stage_input={"contract": self.policy["segment_contract"],
                             "phase": "metadata_synthesis",
                             "unit_id": summary_spec.unit_id, "arguments": args})
            validate_schema(native, summary_schema())
            global_review = {"schema": "tiger.document-global-review/v1",
                "contract": self.policy["segment_contract"], "document_id": raw.doc_id,
                "source_sha256": text_digest(raw.content),
                "manifest_sha256": semantic_hash(manifest),
                "segment_review_sha256": [row["review_sha256"] for row in review_receipts],
                "summary_sha256": semantic_hash(native["summary_zh"]),
                "complete": review_accepted,
                "checks": {**{key: True for key in (
                    "whole_original_coverage", "metadata_synthesis")},
                    "segment_reviews": review_accepted}}
            _same(value, {"phase": "metadata_synthesis", "document_id": raw.doc_id,
                "native": native, "summary_zh": native["summary_zh"],
                "global_review": global_review}, stage)
            document = merge_segment_annotations(
                raw, annotations, summary_zh=native["summary_zh"],
                limits=merged_annotation_limits(len(segments)), target_ids=legacy.TARGET_IDS)
            validate_annotation(document, self.replay.cutoff, **validation_context)
        source_complete = raw.meta.get("completeness") == "complete"
        disposition = ("eligible" if review_accepted and source_complete else "unverifiable")
        _same(result["dispositions"], {raw.doc_id: {"state": disposition,
            "reason": (("all_segments_reviewed_source_qualification_separate"
                        if plan is not None else
                        "semantic_review_complete_source_qualification_separate")
                       if disposition == "eligible" else
                       "independent_review_or_source_qualification_incomplete")}}, stage)
        self.document_hashes[raw.doc_id] = text_digest(raw.content)
        eligible = document if disposition == "eligible" else None
        return (eligible, len(specs)) if return_count else eligible

    @_boundary
    def verify_empty(self, call):
        from .work_required import _SYSTEM

        args = {"system": _SYSTEM, "user": "本轮无原件，不编造语义；返回documents为空数组。",
            "schema_hint": {}, "schema": {"type": "object", "properties": {"documents": {
                "type": "array", "items": {"type": "object"}, "minItems": 0, "maxItems": 0}},
                "required": ["documents"], "additionalProperties": False},
            "public_payload": {"contract": WORK_CLEAN_CONTRACT, "phase": "empty",
                               "assessment": self.replay.assessment, "documents": []}}
        spec = UnitSpec("clean-empty", "document.empty", {"arguments": args},
            {"schema": WORK_CLEAN_CONTRACT, "validator": "empty-clean/v1", "stage": "clean.llm_semantic"})
        output, native, result = self.verify_join("clean.llm_semantic", call, spec, args, stage_input=spec.inputs)
        _same(output, {"documents": []}, "clean.llm_semantic")
        _same(native, output, "clean.llm_semantic")
        _same(result["dispositions"], {}, "clean.llm_semantic")

    @_boundary
    def verify_snapshot(self, reference, *, docs, frontier, bundle, source_registry):
        manifest = self.store.manifest
        expected = {"schema": SNAPSHOT_CONTRACT,
            **{key: manifest[key] for key in ("run_id", "tenant_id", "source_sha256",
                                             "policy_sha256", "input_sha256", "as_of")},
            "research_policy": deepcopy(self.policy), "frontier": deepcopy(frontier),
            "bundle_id": bundle["bundle_id"], "bundle_sha256": bundle["manifest_sha256"],
            "source_registry_sha256": source_registry["sha256"],
            "documents": [{"id": doc.raw.doc_id, "raw_sha256": value_hash(asdict(doc.raw)),
                "content_sha256": text_digest(doc.raw.content),
                "annotation_sha256": value_hash(annotation_payload(doc)),
                "eligibility_sha256": value_hash(doc.eligibility),
                "conflict_ids": sorted({identity for row in doc.eligibility.get("claims", [])
                                         for identity in row.get("conflict_ids", [])})} for doc in docs],
            "coverage": deepcopy(bundle["gaps"])}
        doc_hashes = {doc.raw.doc_id: text_digest(doc.raw.content) for doc in docs}
        if (len(doc_hashes) != len(docs)
                or any(doc_id not in self.document_hashes or sha256 != self.document_hashes[doc_id]
                       for doc_id, sha256 in doc_hashes.items())
                or not isinstance(reference, dict) or set(reference) != {"schema", "manifest_sha256", "ref"}
                or reference["schema"] != SNAPSHOT_CONTRACT
                or reference["manifest_sha256"] != value_hash(expected)):
            _fail("intelligence.freeze")
        _same(self.store.read_artifact(reference["ref"], schema=SNAPSHOT_CONTRACT), expected, "intelligence.freeze")
        self.snapshot, self.snapshot_reference = expected, deepcopy(reference)
        return expected

    @_boundary
    def assert_complete(self):
        self.store.verify()
        status = self.store.status()
        if (status["graph_state"] != "completed" or status["unknown_attempts"]
                or {row["unit_id"] for row in status["units"]} != self.verified_units
                or self.store.document_dispositions()["documents"].keys() != self.document_hashes.keys()
                or status["format_reanswers"] > 1):
            _fail()
        observations = [row for row in self.replay.chat_observations if row["call_id"] in self.work_call_ids]
        amount = {key: sum(row["usage"][key] for row in observations if row["usage"] is not None)
                  for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if (len(observations) != status["settled_attempts"]
                or any(row["usage"] is None for row in observations)
                or amount != status["known_usage"]
                or sum(row["ordinal"] == 2 for row in observations) != status["format_reanswers"]):
            _fail()
        return {"schema": SUMMARY_SCHEMA, "manifest_sha256": self.store.manifest_sha256,
            "verified_units": len(self.verified_units), "document_count": len(self.document_hashes),
            "native_count": len(observations), "known_usage": amount,
            "format_reanswers": status["format_reanswers"],
            "snapshot_sha256": None if self.snapshot_reference is None else self.snapshot_reference["manifest_sha256"]}


def work_proof(replay):
    value = getattr(replay, "_research_work_proof", None)
    if value is None:
        value = ResearchWorkProof(replay)
        replay._research_work_proof = value
    return value


@_boundary
def verified_research_scope(replay):
    """A supplementary scope is frozen authority for a fresh admission only."""
    from datetime import datetime
    from ..research_followup import validate_scope

    verifier = work_proof(replay)
    value = replay.admission.get("research_scope_sha256")
    if value != replay.policy.get("research_scope_sha256"):
        _fail("intelligence.frontier")
    if value is None:
        if (replay.run / "research-scope.json").exists():
            _fail("intelligence.frontier")
        return None
    scope = validate_scope(read_json(replay.run, "research-scope.json"))
    if (scope["manifest_sha256"] != value
            or scope["paper_account"] != verifier.store.manifest["tenant_id"]
            or replay.cutoff < datetime.fromisoformat(scope["requested_at"])
            or scope["parent_run_id"] == replay.run.name):
        _fail("intelligence.frontier")
    return scope


@_boundary
def verify_source_index(actual, reported, documents):
    """Rebuild each original's public locator; duplicate versions stay visible."""
    seen, expected = set(), []
    for document in documents:
        raw = document.raw
        if raw.doc_id in seen:
            _fail("report.emit")
        seen.add(raw.doc_id)
        expected.append({"id": raw.doc_id, "title": raw.title, "url": raw.url,
            "published_at": raw.published or None, "source_id": raw.source,
            "content_sha256": text_digest(raw.content),
            "completeness": raw.meta.get("completeness", "unknown")})
    _same(actual.get("source_index"), expected, "report.emit")
    _same(reported.get("source_index"), expected, "report.emit")
    return expected


@_boundary
def verify_report_identity(actual, reported, admission):
    _same(actual.get("run_id"), admission["run_id"], "report.emit")
    _same(reported.get("run_id"), admission["run_id"], "report.emit")


@_boundary
def source_usage(attempts):
    """Recompute concurrent-source reservations without discarding old holds."""
    result = {"requests": sum(row["result"]["request_count"] for row in attempts),
        "fetches": sum(row["operation"] == "fetch" for row in attempts),
        "bytes": sum(row["result"]["bytes_received"] for row in attempts),
        "unreconciled_attempts": sum(bool(row["result"]["usage"].get("requires_reconciliation")) for row in attempts)}
    reserved = [row for row in attempts if "reserved_bytes" in row["request"]]
    if reserved:
        held = [row for row in reserved if row["status"] in {"inflight", "unknown"}]
        committed = [row for row in attempts if not ("reserved_bytes" in row["request"]
                     and row["status"] in {"inflight", "unknown"})]
        result.update(reserved_bytes=sum(row["request"]["reserved_bytes"] for row in held),
            reserved_requests=sum(row["request"]["reserved_requests"] for row in held),
            committed_bytes=sum(row["result"].get("bytes_received", 0) for row in committed),
            committed_requests=sum(row["result"].get("request_count", row["request"].get("reserved_requests", 1))
                                   for row in committed))
    return result


@_boundary
def source_runtime_from_events(events, *, configured_workers, concurrency_limit):
    """Bind the closed operational receipt to its original start/finish events.

    This checks the recorded counter and its legal bounds. The receipt is not
    independent evidence that different HTTP connections overlapped in time.
    """
    pending = latest = None
    expected = {"schema": "tiger.source-runtime/v1", "configured_workers": configured_workers,
        "admitted_workers": min(configured_workers, concurrency_limit),
        "peak_source_operations": 0, "active_source_operations": 0,
        "paid_source_policy": "serial_with_reconciliation_barrier", "host_policy": "durable_per_http_host"}
    for event in events:
        if event["kind"] == "source_runtime_started":
            if pending is not None:
                _fail()
            _same(event["payload"], expected)
            pending = event
        elif event["kind"] == "source_runtime_finished":
            value = event["payload"]
            if (pending is None or not isinstance(value, dict) or set(value) != set(expected)
                    or type(value["peak_source_operations"]) is not int
                    or not 0 <= value["peak_source_operations"] <= expected["admitted_workers"]):
                _fail()
            _same({**value, "peak_source_operations": 0}, expected)
            latest, pending = deepcopy(value), None
    if pending is not None:
        _fail()
    return latest


@_boundary
def partial_work_accounting(replay, *, parent_accounting=None):
    """Count bound original replies without certifying failed task semantics.

    Every append-only observation is retained, including late replies that
    still need reconciliation. A durable intent without a native reply keeps
    its reservation. Repeated parent joins and repeated identical observations
    cannot charge the same provider response twice.
    """
    from ..tasks.contracts import OBSERVATION_SCHEMA
    from .intelligence_proof import _partial_chat_request
    from .json_observation import replay_attempt
    from .rejected_native import response_identity

    verifier = work_proof(replay)
    store = verifier.store
    with store._tx() as connection:
        attempts = sorted(store._rows(connection, "attempts"),
                          key=lambda row: (row["unit_id"], row["ordinal"], row["attempt_id"]))
        observations = store._rows(connection, "observations")
        retry_claims = store._rows(connection, "retry_claims")
    by_attempt = {}
    for row in observations:
        by_attempt.setdefault(row["attempt_id"], []).append(row)
    by_unit = {}
    for row in attempts:
        by_unit.setdefault(row["unit_id"], []).append(row)
    for unit_id, unit_attempts in by_unit.items():
        if [row["ordinal"] for row in unit_attempts] not in ([1], [1, 2]):
            _fail()
        if len(unit_attempts) == 2:
            first, second = unit_attempts
            request = store.read_attempt_request(first["attempt_id"])
            if (retry_claims != [{"slot": 1, "unit_id": unit_id, "attempt_id": first["attempt_id"]}]
                    or first["state"] != "settled"
                    or store.read_attempt_request(second["attempt_id"]) != request
                    or not replay_attempt(store.read_attempt_response(first["attempt_id"]), request)["retry_eligible"]):
                _fail(request["stage"])
    calls, response_owners, known, unknown_native, rows, reservations = {}, {}, {}, set(), [], []
    native_records = {}
    for attempt in attempts:
        identity = attempt["attempt_id"]
        unit = store.unit(attempt["unit_id"])
        request = store.read_attempt_request(identity)
        stage = unit["spec"]["contract"]["stage"]
        if (attempt["operation"] != "model.deepseek.chat" or request["stage"] != stage
                or request.get("response_observation_contract") != replay.observation_contract
                or type(request.get("call_id")) is not str
                or re.fullmatch(r"[0-9a-f]{32}", request["call_id"]) is None
                or request["call_id"] in calls and calls[request["call_id"]] != attempt["unit_id"]):
            _fail(stage)
        calls[request["call_id"]] = attempt["unit_id"]
        _partial_chat_request(request, stage, replay.scene_policy_path,
                              counter_contract=replay.budget_contract,
                              rejected_native_contract=verifier.policy["rejected_native_contract"])
        attached, native_rows = None, []
        for observation in sorted(by_attempt.get(identity, []), key=lambda item: item["observation_id"]):
            envelope = store.read_artifact(observation["ref"], schema=OBSERVATION_SCHEMA)
            if (set(envelope) != {"schema", "manifest_sha256", "attempt_id", "request_sha256", "response", "usage"}
                    or envelope["schema"] != OBSERVATION_SCHEMA
                    or envelope["manifest_sha256"] != store.manifest_sha256
                    or envelope["attempt_id"] != identity
                    or envelope["request_sha256"] != attempt["request_ref"]["sha256"]
                    or envelope["usage"] != observation["usage"]):
                _fail(stage)
            native = envelope["response"]
            if native["ordinal"] != attempt["ordinal"]:
                _fail(stage)
            verdict = replay_attempt(native, request)
            usage = verdict["usage"]
            amount = None if usage is None else {key: usage[key] for key in
                ("prompt_tokens", "completion_tokens", "total_tokens")}
            _same(amount, envelope["usage"], stage)
            response_id, native_hash = response_identity(native)
            row = {"observation_id": observation["observation_id"], "native_ref": observation["ref"],
                "provider_response_id": response_id, "usage_known": usage is not None,
                "usage": amount, "diagnostic": verdict["diagnostic"], "late": bool(observation["late"])}
            native_rows.append(row)
            if observation["observation_id"] == attempt["observation_id"]:
                attached = row
            if usage is not None:
                owner = response_owners.get(response_id)
                binding = (identity, native_hash, amount)
                if owner is not None and owner != binding:
                    _fail(stage)
                response_owners[response_id] = binding
                known[(identity, response_id)] = amount
                native_records[(request["call_id"], attempt["ordinal"], response_id)] = {
                    "attempt_id": identity, "usage": amount}
        if attempt["observation_id"] is not None and attached is None:
            _fail(stage)
        if attempt["state"] == "settled" and (attached is None or not attached["usage_known"]
                or attached["usage"] != attempt["usage"]):
            _fail(stage)
        unresolved = attempt["state"] in {"dispatched", "observed"}
        if unresolved and (attached is None or not attached["usage_known"]):
            unknown_native.add(identity)
        if attempt["state"] not in {"settled", "not_sent"}:
            reservations.append({"attempt_id": identity, "unit_id": attempt["unit_id"],
                "call_id": request["call_id"], "ordinal": attempt["ordinal"],
                "dispatch_state": "not_sent" if attempt["state"] == "prepared" else attempt["state"],
                "ledger_state": attempt["state"], "reservation": deepcopy(attempt["reservation"]),
                "requires_reconciliation": unresolved})
        rows.append({"attempt_id": identity, "unit_id": attempt["unit_id"], "stage": stage,
            "call_id": request["call_id"], "ordinal": attempt["ordinal"],
            "request_sha256": request["request_sha256"], "request_ref": attempt["request_ref"],
            "dispatch_state": "not_sent" if attempt["state"] == "prepared" else attempt["state"],
            "ledger_state": attempt["state"],
            "provider_response_id": None if attached is None else attached["provider_response_id"],
            "usage_known": attached is not None and attached["usage_known"],
            "usage": None if attached is None else attached["usage"],
            "native_ref": None if attached is None else attached["native_ref"],
            "native_observations": native_rows})
    parent = [] if parent_accounting is None else parent_accounting.get("observed_attempts", [])
    parent_known, parent_unknown, parent_reanswers = set(), set(), set()
    for row in parent:
        if row["call_id"] not in calls:
            continue
        key = (row["call_id"], row["ordinal"], row.get("response_id"))
        if row.get("usage") is not None:
            if key not in native_records:
                _fail(row["stage"])
            _same({name: row["usage"][name] for name in
                  ("prompt_tokens", "completion_tokens", "total_tokens")}, native_records[key]["usage"], row["stage"])
            parent_known.add((native_records[key]["attempt_id"], row["response_id"]))
            if row["ordinal"] == 2:
                parent_reanswers.add(native_records[key]["attempt_id"])
        else:
            matching = [attempt for attempt in rows if attempt["call_id"] == row["call_id"]
                        and attempt["ordinal"] == row["ordinal"]]
            if len(matching) != 1 or matching[0]["attempt_id"] not in unknown_native:
                _fail(row["stage"])
            parent_unknown.add(matching[0]["attempt_id"])
            if row["ordinal"] == 2:
                parent_reanswers.add(matching[0]["attempt_id"])
    def totals(values):
        frozen = tuple(values)
        return {name: sum(value[name] for value in frozen)
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for row in rows:
        row["joined_parent_counted"] = (row["attempt_id"], row["provider_response_id"]) in parent_known
    unknown_ids = sorted(unknown_native)
    status = store.status()
    return {"schema": ACCOUNTING_SCHEMA, "run_id": replay.run.name,
        "manifest_sha256": store.manifest_sha256, "semantics_state": "unverified",
        "business_completed": False, "all_required_completed": False,
        "accounting_scope": "research_work_chat_completions_only",
        "attempt_count": len(attempts), "known_native_attempts": len(known),
        "known_usage": totals(known.values()) if known else None,
        "known_usage_settled": status["known_usage_observed"],
        "unknown_attempts": len(unknown_ids), "unknown_attempt_ids": unknown_ids,
        "requires_reconciliation": any(row["requires_reconciliation"] for row in reservations),
        "unknown_reservations": reservations,
        "format_reanswers": status["format_reanswers"],
        "already_parent_counted_native_attempts": len(parent_known),
        "additional_known_native_attempts": len(set(known) - parent_known),
        "additional_known_usage": totals(value for key, value in known.items() if key not in parent_known),
        "additional_unknown_attempts": len(unknown_native - parent_unknown),
        "additional_format_reanswers": status["format_reanswers"] - len(parent_reanswers),
        "attempts": rows}


@_boundary
def merge_partial_work_accounting(replay, parent):
    """A new top-level version keeps parent snapshots and ledger refs distinct."""
    work = partial_work_accounting(replay, parent_accounting=parent)
    known_count = parent["known_native_attempts"] + work["additional_known_native_attempts"]
    unknown = parent["unknown_attempts"] + work["additional_unknown_attempts"]
    requires_reconciliation = parent["requires_reconciliation"] or work["requires_reconciliation"]
    known = {key: (parent["known_usage"] or {}).get(key, 0) + work["additional_known_usage"][key]
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")} if known_count else None
    result = {key: value for key, value in parent.items() if key != "observed_attempts"}
    result.update(schema="tiger.failed-model-accounting/v4", semantics_state="unverified",
        parent_observed_attempts=deepcopy(parent["observed_attempts"]), research_work=work,
        native_count=known_count + unknown, observed_attempts_count=known_count + unknown,
        known_native_attempts=known_count, known_usage=known, unknown_attempts=unknown,
        requires_reconciliation=requires_reconciliation,
        observation_state="usage_unknown" if unknown else "known_usage" if known_count else "no_chat_observation",
        format_reanswers=parent["format_reanswers"] + work["additional_format_reanswers"],
        request_binding="explicit-parent-IO-and-work-ledger-native-complete-wire-window",
        scope="Failed research-work accounting only; document, research and business semantics remain unverified.")
    return result


@_boundary
def verify_analysis_call(replay, stage, docs, targets):
    from .required_schemas import (sentiment_schema, risk_schema, narrative_schema,
                                   validate_target_assessments)
    from ..search.event_identity import target_evidence_sources, target_evidence_coverage

    verifier = work_proof(replay)
    if verifier.snapshot_reference is None:
        _fail(stage)
    sources, coverage = target_evidence_sources(docs, targets), target_evidence_coverage(docs, targets)
    schema = {"tech.sentiment": sentiment_schema, "tech.risk": risk_schema,
              "sector.narrative": narrative_schema}[stage](targets, [doc.raw.doc_id for doc in docs],
                  allow_empty=True, target_sources=sources, target_coverage=coverage)
    common = {"etfs" if stage == "sector.narrative" else "chains": list(targets),
              "target_sources": sources, "target_coverage": coverage}
    arguments = None
    for compact in (False, True):
        candidate = legacy.chat_arguments(stage, {**common, **task_evidence_view(docs, compact=compact)},
                                           schema, replay.assessment)
        try:
            verifier.plan(stage, candidate)
        except TokenBudgetError as error:
            if error.code != "INPUT_CONTEXT_EXCEEDED":
                raise
        else:
            arguments = candidate
            break
    if arguments is None:
        _fail(stage)
    snapshot = verifier.snapshot_reference["manifest_sha256"]
    dependencies = tuple(sorted(row["unit_id"] for row in verifier.store.status()["units"]
        if row["spec"]["terminal_documents"] or row["spec"]["kind"] == "document.empty"))
    name = "analysis-" + stage + "-" + snapshot[:24]
    inputs = {"arguments": arguments, "snapshot_sha256": snapshot,
              "documents_sha256": semantic_hash([asdict(doc) for doc in docs]), "targets": list(targets)}
    spec = UnitSpec(name, "research.analysis", inputs,
        {"schema": RESEARCH_WORK_CONTRACT, "validator": "target-analysis/v1", "stage": stage},
        dependencies=dependencies)
    calls = list(replay.io(stage))
    if len(calls) != 1:
        _fail(stage)
    output, native, result = verifier.verify_join(stage, calls[0][0], spec, arguments, stage_input=inputs)
    _same(output, {"stage": stage, "snapshot_sha256": snapshot, "native": native}, stage)
    _same(result["dispositions"], {}, stage)
    validate_target_assessments(stage, native, targets, sources, target_coverage=coverage)
    return native


@_boundary
def verify_decision_call(replay, call, payload, schema, *, numeric_context, account_usd):
    verifier = work_proof(replay)
    if verifier.snapshot_reference is None:
        _fail("decision.debate")
    stage = "decision.debate"
    arguments = normalize(legacy.chat_arguments(stage, payload, schema, replay.assessment))
    snapshot = verifier.snapshot_reference["manifest_sha256"]
    inputs = {"arguments": arguments, "snapshot_sha256": snapshot,
        "account_version_before": (replay.admission["parent"] or {}).get("version_sha256"),
        "numeric_context": normalize(numeric_context), "account_usd": account_usd}
    dependencies = tuple(sorted("analysis-" + name + "-" + snapshot[:24]
        for name in ("tech.sentiment", "tech.risk", "sector.narrative")))
    spec = UnitSpec("decision-" + semantic_hash(arguments)[:32], "research.decision", inputs,
        {"schema": RESEARCH_WORK_CONTRACT, "validator": "numeric-bound-decision/v1", "stage": stage},
        dependencies=dependencies)
    output, native, result = verifier.verify_join(stage, call, spec, arguments, stage_input=inputs)
    _same(result["dispositions"], {}, stage)
    if getattr(verifier, "decision_output", None) is not None:
        _fail(stage)
    verifier.decision_output = output
    return native


@_boundary
def verify_decision_outcome(replay, result, *, selected_symbols, pick_rationale):
    verifier = work_proof(replay)
    output = result["model_output"]
    expected = {"result": result, "snapshot_sha256": verifier.snapshot_reference["manifest_sha256"],
        "selected_symbols": selected_symbols, "action": output["action"],
        "pick_rationale": pick_rationale, "market_view": output["action_summary"],
        "action_summary": output["action_summary"], "execution_decisions": result["execution_decisions"]}
    _same(getattr(verifier, "decision_output", None), expected, "decision.debate")


@_boundary
def verify_clean(replay, registry, archive, bundle, frontier, plan, snapshots, phases):
    from ..intelligence import project_to_rawdocuments
    from ..research_risk import assessment_rows
    from ..research_relations import validate_research_events
    from ..search.credibility import scoring_docs
    from ..search.event_identity import STRUCTURED_EVENT_CONTRACT
    from .intelligence_proof import _normalized_assessment, _project_gaps

    stage = "clean.llm_semantic"
    verifier = work_proof(replay)
    calls = [call for call, _, _ in replay.io(stage) if call["method"] == "tasks.child.join"]
    if any(call["method"] == "model.deepseek.chat" for call, _, _ in replay.io(stage)):
        _fail(stage)
    rounds = snapshots[1]["rounds"]
    if not 1 <= len(rounds) <= legacy.TOOL_LIMITS["rounds"]:
        _fail(stage)
    cleaned, identities, index, previous_gaps, round_count = [], {}, 0, None, 0
    for number in range(legacy.TOOL_LIMITS["rounds"]):
        current = validate_raw_documents(project_to_rawdocuments(phases[0 if number == 0 else 1]))
        fresh = []
        for raw in current:
            identity = semantic_hash(asdict(raw))
            if raw.doc_id in identities and identities[raw.doc_id] != identity:
                _fail(stage)
            if raw.doc_id not in identities:
                fresh.append(raw)
                identities[raw.doc_id] = identity
        if number and not fresh:
            break
        context = {"security_ids": frontier["security_ids"],
            "entity_registry": snapshots[0 if number == 0 else 1]["entity_registry"],
            "source_policies": registry.public_manifest()}
        if fresh:
            for raw in fresh:
                document, consumed = verifier.verify_document(
                    raw, calls[index:], validation_context=context, return_count=True)
                if document is not None:
                    cleaned.append(document)
                index += consumed
        else:
            verifier.verify_empty(calls[index])
            index += 1
        stats = validate_research_events(cleaned, replay.cutoff, **context)
        assessment = assessment_rows(cleaned, plan)
        gaps = sorted(row["cell_id"] for row in assessment["coverage"] if row["status"] != "present")
        stop = "coverage_complete" if not gaps else "no_progress" if gaps == previous_gaps else None
        _same(rounds[number], {"round": number, "document_ids": [doc.doc_id for doc in fresh],
            "model_batches": [[doc.doc_id] for doc in fresh], "assessment": assessment, "status": stats,
            "work_contract": RESEARCH_WORK_CONTRACT, "stop_reason": stop}, stage)
        round_count += 1
        if not gaps or gaps == previous_gaps or number + 1 == legacy.TOOL_LIMITS["rounds"]:
            break
        previous_gaps = gaps
    if round_count != len(rounds) or index != len(calls):
        _fail(stage)
    _same(replay.value(stage), {"documents": cleaned, "degraded_count": 0, "acquisition": snapshots[1]}, stage)
    _same(replay.inputs["clean.cross_validate"], {"documents": cleaned,
        "event_contract": STRUCTURED_EVENT_CONTRACT, "security_ids": frontier["security_ids"]}, "clean.cross_validate")
    stats = validate_research_events(cleaned, replay.cutoff, security_ids=frontier["security_ids"],
        entity_registry=snapshots[1]["entity_registry"], source_policies=registry.public_manifest())
    scored = scoring_docs(cleaned)
    _same(replay.value("clean.cross_validate"), {"documents": cleaned, "scoring_documents": scored, "stats": stats}, "clean.cross_validate")
    _same(replay.inputs["intelligence.freeze"], {"frontier_hash": frontier["manifest_sha256"],
        "documents": cleaned, "stats": stats}, "intelligence.freeze")
    assessment = _normalized_assessment(assessment_rows(cleaned, plan))
    _same(bundle["claims"], assessment["claims"], "intelligence.freeze")
    _same(bundle["coverage"], assessment["coverage"], "intelligence.freeze")
    gaps = [{**row, "reason": row.get("reason", "DETERMINISTIC_ASSESSMENT"), "required": cell["required"]}
            for row, cell in zip(assessment["coverage"], plan["coverage_cells"], strict=True)]
    observed = [{key: value for key, value in row.items() if key not in {"source_diagnostics", "source_reason"}}
                for row in bundle["gaps"]]
    _same(observed, gaps, "intelligence.freeze")
    events = [event for event in archive.events(bundle["job_id"])
              if event["sequence"] <= bundle["event_head"]["sequence"]]
    _same(bundle["gaps"], _project_gaps(events), "intelligence.freeze")
    missing = [row["cell_id"] for row in gaps if row["required"] and row["status"] != "present"]
    _same(bundle["missing_required"], missing, "intelligence.freeze")
    if bundle["state"] != ("partial" if missing else "completed"):
        _fail("intelligence.freeze")
    verifier.verify_snapshot(replay.value("intelligence.freeze")["research_snapshot"], docs=cleaned,
        frontier=frontier, bundle=bundle, source_registry=registry.public_manifest())
    return cleaned, scored, stats
