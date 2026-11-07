"""Read-only replay of the v2 acquisition and mandatory model boundary.

Stored eligibility, coverage, success flags and semantic summaries are outputs,
never authorities. This verifier reconstructs them from frozen source bytes and
model receipts. The market-only frontier, evidence-aware fusion, two momentum
stages and six scoring/approval stages are recomputed over sealed observations.
No provider, source adapter or model may open a network connection here.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import ast
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..agent_evidence import (EvidenceInvalid, normalize, read_bytes, read_json,
                              read_snapshot, strict_json, verify_events, checked_root, safe_path,
                              digest, json_bytes)
from ..cleaning.pipeline import rule_base_clean
from ..intelligence import (build_plan, default_registry,
                            project_to_rawdocuments, replay_observation, verify_bundle)
from ..intelligence.archive import Archive
from ..intelligence.contracts import IntelligenceError, SearchRequest, SearchResult, safe_value, source_time
from ..intelligence.planner import compile_requests
from ..intelligence.registry import acquisition_mode
from ..intelligence.adapters import deepseek_query, _hit
from ..intelligence_workflow import (ACQUISITION_CONTRACT, SOURCE_CONTENT_CONTRACT,
    SEARCH_MODE, TOOL_LIMITS, entity_registry, restore_frame, validate_input_packet,
    verify_frontier, acquisition_policy, value_hash)
from ..intelligence_workflow import validate_baseline_manifest
from ..redline import LLMUnavailable
from ..redline.execution import (REQUIRED_MODEL_STAGES, RequiredModelFailed,
                                 daily_stage_contract, ExecutionContractError)
from ..redline.model_diagnostics import validate_model_diagnostic
from ..search.credibility import evidence_for, scoring_docs
from ..search.event_identity import (STRUCTURED_EVENT_CONTRACT, validate_structured_events,
    target_evidence_sources, target_evidence_coverage)
from ..search.models import StructuredDocument
from ..tech_chain.agents import (ChainMonitorRow, GlobalLinkageRow, LinkMomentum,
                                 RiskAlert, StructuredSentimentRow)
from ..tech_chain.fusion import TechChainFusionAgent, signals_to_bonus_map
from ..tech_chain.risk_evidence import TARGET_CONTRACT as RISK_CONTRACT, risk_coverage
from ..tech_chain.universe import TECH_SUBCHAINS, CHAIN_TOPICS, SECTOR_TOPICS
from . import intelligence_required as semantic
from .deepseek_required import (MODEL, RELEASE, SEARCH_MODE as NATIVE_SEARCH_MODE,
    DeepSeekRequiredClient, sanitize_model_text, sanitize_model_value, sanitize_url, validate_schema)
from .model_budget import (CONTEXT_TOKENS, POLICY_VERSION, REGISTRY_VERSION,
    TOKENIZER_SHA256, TokenBudgetError, chat_request_core, plan_request)
from .required_schemas import (sentiment_schema, risk_schema, narrative_schema,
                               decision_schema, validate_target_assessments)
from .required_proof import (_hash, _plain, _same_watch_facts, _verify_search_request,
                             _verify_search_rows)
from .scene_policy import KERNEL_SCENE_MAP, scene_config, ScenePolicyError

LEGACY_CLEAN_SOURCE_SHA256 = "dc15de6306fe8e134df343ea42a0bf8073b4a7408e8c6eca2b3e418022a61c3f"
SUMMARY_CLEAN_SOURCE_SHA256 = "3db582964bbfa8a709ff3c40c5f72ec4012b1f6a06b74f6c5e57e5a4771b5008"
SNAPSHOT_ONLY_SUMMARY_CLEAN_SOURCE_SHA256 = "8d3154751d3b92e0024590cb9d2226d68638a59156a81ba0ecd626eec9845222"
IDENTIFIERS_CLEAN_SOURCE_SHA256 = "6cea0fe5533469d8888ecde8e7fd9bb853c7c1dea544136c0dd2655292b61f36"


def _fail(stage=None):
    raise EvidenceInvalid("MODEL_REQUIRED_FAILED", "V2 frozen intelligence evidence is missing or differs", stage=stage)


def _same(actual, expected, stage=None):
    if normalize(actual) != normalize(expected):
        _fail(stage)


def frozen_clean_format_contract(run):
    """Recognize only known frozen prompt code, without importing/executing it."""
    stage = "clean.llm_semantic"
    content = read_bytes(Path(run), "source/trading_system/llm/intelligence_required.py")
    observed = hashlib.sha256(content).hexdigest()
    if observed == LEGACY_CLEAN_SOURCE_SHA256:
        return semantic.LEGACY_CLEAN_FORMAT_CONTRACT
    if observed in {SUMMARY_CLEAN_SOURCE_SHA256, SNAPSHOT_ONLY_SUMMARY_CLEAN_SOURCE_SHA256}:
        return semantic.SUMMARY_CLEAN_FORMAT_CONTRACT
    if observed == IDENTIFIERS_CLEAN_SOURCE_SHA256:
        return semantic.IDENTIFIERS_CLEAN_FORMAT_CONTRACT
    trusted = Path(semantic.__file__).read_bytes()
    if observed != hashlib.sha256(trusted).hexdigest():
        _fail(stage)
    try:
        tree = ast.parse(content.decode("utf-8"))
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "CLEAN_FORMAT_CONTRACT"
                    for target in node.targets)]
        if (len(values) != 1 or not isinstance(values[0], ast.Constant)
                or values[0].value != semantic.CLEAN_FORMAT_CONTRACT):
            _fail(stage)
    except (UnicodeError, SyntaxError, TypeError, ValueError):
        _fail(stage)
    return semantic.CLEAN_FORMAT_CONTRACT


def _request(parameters):
    return SearchRequest(**{key: tuple(value) if key in {"domains", "entities", "targets"} else value
                            for key, value in parameters.items()})


def _source_parameters(spec, operation, parameters):
    """Remove only the internally recorded, trusted extractor policy field.

    Every other field remains in the request identity and must still match the
    independently compiled plan. A page cannot nominate its own parser policy.
    """
    if operation == "search":
        context = parameters.get("context", {})
        if "_parse_policy" not in context:
            return parameters
        recorded = context["_parse_policy"]
        result = {**parameters, "context": {key: value for key, value in context.items() if key != "_parse_policy"}}
    else:
        if "_parse_policy" not in parameters:
            return parameters
        recorded = parameters["_parse_policy"]
        result = {key: value for key, value in parameters.items() if key != "_parse_policy"}
    from ..intelligence.parse_limits import ParsePolicy
    trusted = spec.options.get("extractor")
    if trusted is None or recorded != ParsePolicy.from_value(trusted).public():
        _fail()
    return result


class _Replay:
    def __init__(self, run, admission, scene_policy_path, *, _partial=False):
        self.run, self.admission, self.scene_policy_path = Path(run), admission, scene_policy_path
        clock = admission["clock"]
        self.cutoff = datetime.fromisoformat(clock["admitted_at_utc"])
        run_date = clock["run_date"]
        if (date.fromisoformat(run_date).isoformat() != run_date
                or clock["market_timezone"] != "America/New_York" or self.cutoff.utcoffset() is None
                or self.cutoff.astimezone(ZoneInfo("America/New_York")).date().isoformat() != run_date):
            _fail()
        self.assessment = {key: clock[key] for key in ("run_date", "market_timezone")}
        self.policy = read_json(self.run, "policy.json") if (self.run / "policy.json").exists() else None
        self.authoritative_paper = (self.policy is not None and
            self.policy.get("paper_ledger_contract") == "tiger.paper-execution.v1")
        if self.policy is not None and self.policy.get("paper_ledger_contract") not in {None, "tiger.paper-execution.v1"}:
            _fail()
        contract = daily_stage_contract(True, intelligence=True, authoritative_paper=self.authoritative_paper)
        if self.policy is not None:
            _same(self.policy["stages"], contract)
        if _partial:
            events = _partial_events(self.run, contract)
        else:
            verify_events(self.run, self.run.name, contract)
            events = [strict_json(line) for line in read_bytes(self.run, "events.jsonl").splitlines()]
        self.events = events
        self.ends = {row["stage"]: row for row in events if row["kind"] == "end"}
        self.outputs = {stage: self.snapshot(row, "output") for stage, row in self.ends.items()}
        self.inputs = {stage: self.snapshot(row, "input") for stage, row in self.ends.items()}
        for stage, output in self.outputs.items():
            for call in output["io_calls"]:
                method = call["method"]
                if ((method.startswith("model.deepseek.") and stage not in {"search.collect", *REQUIRED_MODEL_STAGES})
                        or method == "intelligence.source.request" and stage not in {"search.collect", "clean.llm_semantic"}):
                    _fail(stage)
        self.response_ids, self.call_ids, self.identities = [], set(), set()
        self.counts = {stage: 0 for stage in REQUIRED_MODEL_STAGES}
        self.chat_schema_retries = 0
        self.observation_contract = (self.policy or {}).get("model_contract", {}).get(
            "response_observation_contract", "")
        self.observation_refs = set()
        self.chat_observations = []
        self.native_searches, self.native_fetches = [], []
        self.attempt_stages = {}

    def snapshot(self, row, role):
        return read_snapshot(self.run, row[role + "_ref"], run_id=self.run.name,
                             stage=row["stage"], role=role)

    def value(self, stage):
        return self.outputs[stage]["value"]

    def io(self, stage):
        for call in self.outputs[stage]["io_calls"]:
            request = read_snapshot(self.run, call["input_ref"], run_id=self.run.name, stage=stage, role="input")
            response = read_snapshot(self.run, call.get("output_ref", call.get("error_ref")),
                                     run_id=self.run.name, stage=stage, role="output")
            yield call, request, response

    def planner(self, stage):
        thinking = scene_config(KERNEL_SCENE_MAP[stage], self.scene_policy_path)["thinking"]
        mode, effort = thinking["type"], thinking.get("effort")
        def plan(payload, schema):
            args = semantic.chat_arguments(stage, payload, schema, self.assessment,
                clean_format_contract=frozen_clean_format_contract(self.run)
                    if stage == "clean.llm_semantic" else semantic.CLEAN_FORMAT_CONTRACT)
            system = sanitize_model_text(args["system"])
            user = sanitize_model_text(args["user"]) + "\n" + json.dumps(
                sanitize_model_value(args["public_payload"]), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False)
            return plan_request(chat_request_core(system=system, user=user, schema=schema,
                schema_hint={}, thinking_mode=mode, effort=effort, temperature=0.2))
        return plan

    def chat(self, stage, call, request, proof, payload, schema):
        if call["method"] != "model.deepseek.chat" or "error_ref" in call:
            _fail(stage)
        call_id = request["call_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", call_id) or call_id in self.call_ids:
            _fail(stage)
        self.call_ids.add(call_id)
        expected_body, estimate, ceiling = self.planner(stage)(payload, schema)
        thinking = scene_config(KERNEL_SCENE_MAP[stage], self.scene_policy_path)["thinking"]
        mode, effort = thinking["type"], thinking.get("effort")
        metadata = {"thinking_mode": mode, "reasoning_effort": effort,
            "input_token_estimate": estimate, "output_ceiling": ceiling,
            "policy_version": POLICY_VERSION, "registry_version": REGISTRY_VERSION,
            "tokenizer_sha256": TOKENIZER_SHA256}
        body = request["request"]
        observed = bool(self.observation_contract)
        if observed:
            from .json_observation import CONTRACT
            if self.observation_contract != CONTRACT or request.get("response_observation_contract") != CONTRACT:
                _fail(stage)
        elif "response_observation_contract" in request or "response_observation" in proof:
            _fail(stage)
        if (request["schema"] != ("tiger.deepseek-request.v2" if observed else "tiger.deepseek-request.v1")
                or request["endpoint"] != "https://api.deepseek.com/chat/completions"
                or request["method"] != "POST" or request["provider"] != "deepseek"
                or request["stage"] != stage or request["release"] != RELEASE
                or request.get("scene") != KERNEL_SCENE_MAP[stage]
                or any(request.get(key) != value or proof.get(key) != value for key, value in metadata.items())
                or body != expected_body or request["request_sha256"] != _hash(body)
                or request["output_schema"] != schema or request["schema_sha256"] != _hash(schema)
                or proof["schema"] != ("tiger.deepseek-call.v2" if observed else "tiger.deepseek-call.v1")
                or proof["provider"] != "deepseek"
                or proof["stage"] != stage or proof["call_id"] != call_id
                or proof.get("scene") != KERNEL_SCENE_MAP[stage]
                or proof["request_sha256"] != request["request_sha256"]
                or proof["schema_sha256"] != request["schema_sha256"]
                or proof["model"] != MODEL or proof["requested_model"] != MODEL
                or proof["release"] != RELEASE or proof["finish_reason"] != "stop"
                or proof["search_mode"] != NATIVE_SEARCH_MODE
                or not re.fullmatch(r"[0-9a-f]{64}", proof["transport_response_sha256"])):
            _fail(stage)
        if observed:
            self._chat_observation(stage, request, proof)
            validate_schema(proof["parsed"], schema)
            self.response_ids.append(proof["response_id"])
            self.counts[stage] += 1
            return proof["parsed"]
        retries = proof.get("retry_diagnostics", [])
        if (type(retries) is not list or self.chat_schema_retries + len(retries) > 1
                or any(validate_model_diagnostic(item)["code"] != "OUTPUT_SCHEMA_MISMATCH"
                       or "schema_keyword" not in item for item in retries)):
            _fail(stage)
        self.chat_schema_retries += len(retries)
        response = proof["response"]
        response_id = proof["response_id"]
        fingerprint = proof["system_fingerprint"]
        identity = hashlib.sha256(response_id.encode()).hexdigest()
        if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", response_id) or identity in self.identities
                or fingerprint is not None and (not isinstance(fingerprint, str)
                    or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", fingerprint) is None)
                or response["id"] != response_id or response["model"] != MODEL
                or response["usage"] != proof["usage"]
                or response["system_fingerprint"] != proof["system_fingerprint"]
                or proof["response_sha256"] != _hash(response)
                or response["choices"] != [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(proof["parsed"], ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"), allow_nan=False)}}]):
            _fail(stage)
        self.identities.add(identity)
        attempt = proof.get("retry_attempt")
        if bool(retries) != (attempt is not None):
            _fail(stage)
        if attempt is not None:
            if (set(attempt) != {"response_id_sha256", "transport_response_sha256", "request_sha256",
                                "finish_reason", "model", "usage"}
                    or not re.fullmatch(r"[0-9a-f]{64}", attempt["response_id_sha256"])
                    or attempt["response_id_sha256"] in self.identities
                    or not re.fullmatch(r"[0-9a-f]{64}", attempt["transport_response_sha256"])
                    or attempt["transport_response_sha256"] == proof["transport_response_sha256"]
                    or attempt["request_sha256"] != request["request_sha256"]
                    or attempt["finish_reason"] != "stop" or attempt["model"] != MODEL):
                _fail(stage)
            self.usage(attempt["usage"], mode, ceiling, stage)
            self.identities.add(attempt["response_id_sha256"])
        self.usage(proof["usage"], mode, ceiling, stage)
        validate_schema(proof["parsed"], schema)
        self.response_ids.append(response_id)
        self.counts[stage] += 1
        return proof["parsed"]

    def _observed_attempts(self, stage, request, observation):
        """Replay only journal-referenced native attempts, never discovered files."""
        from .json_observation import CONTRACT, replay_attempt, validate_reference_observation

        if self.observation_contract != CONTRACT:
            _fail(stage)
        observation = validate_reference_observation(observation)
        if (observation["call_id"] != request["call_id"]
                or observation["request_sha256"] != request["request_sha256"]):
            _fail(stage)
        attempts = observation["attempts"]
        retry_count = len(attempts) - 1
        if observation["retry_used"] != bool(retry_count) or self.chat_schema_retries + retry_count > 1:
            _fail(stage)
        self.chat_schema_retries += retry_count
        results = []
        for ordinal, row in enumerate(attempts, 1):
            ref = row["observation_ref"]
            if row["ordinal"] != ordinal or ref["path"] in self.observation_refs:
                _fail(stage)
            self.observation_refs.add(ref["path"])
            native = read_snapshot(self.run, ref, run_id=self.run.name, stage=stage, role="output")
            if (native["call_id"] != request["call_id"] or native["ordinal"] != ordinal
                    or native["request_sha256"] != request["request_sha256"]):
                _fail(stage)
            replayed = replay_attempt(native, request)
            _same(row["diagnostic"], replayed["diagnostic"], stage)
            if ordinal < len(attempts) and not replayed["retry_eligible"]:
                _fail(stage)
            response = native["response"]
            if response is not None:
                identity = hashlib.sha256(response["id"].encode()).hexdigest()
                if identity in self.identities:
                    _fail(stage)
                self.identities.add(identity)
            elif ordinal != len(attempts):
                _fail(stage)
            results.append((row, native, replayed))
            self.chat_observations.append({"stage": stage, "call_id": request["call_id"],
                "request_sha256": request["request_sha256"], "ordinal": ordinal,
                "observation_ref": ref,
                "response_id": response["id"] if response is not None else None,
                "diagnostic": replayed["diagnostic"], "usage": replayed["usage"]})
        return results

    def _chat_observation(self, stage, request, proof):
        if (proof.get("response_observation_contract") != self.observation_contract
                or "retry_attempt" in proof or "retry_diagnostics" in proof):
            _fail(stage)
        attempts = self._observed_attempts(stage, request, proof["response_observation"])
        _, native, last = attempts[-1]
        if last["diagnostic"] is not None or last["parsed"] is None or last["usage"] is None:
            _fail(stage)
        _same(proof["parsed"], last["parsed"], stage)
        original = native["response"]
        safe_response = {key: original[key] for key in ("id", "object", "model", "system_fingerprint", "usage")}
        safe_response["choices"] = [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(last["parsed"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False)}}]
        if (proof["response"] != safe_response or proof["response_sha256"] != _hash(safe_response)
                or proof["response_id"] != original["id"]
                or proof["system_fingerprint"] != original["system_fingerprint"]
                or proof["usage"] != original["usage"]
                or proof["transport_response_sha256"] != native["transport"]["body_sha256"]):
            _fail(stage)
        for _, _, result in attempts:
            if result["usage"] is None:
                _fail(stage)
            self.usage(result["usage"], request["thinking_mode"], request["output_ceiling"], stage)
        return attempts

    def chat_accounting(self, *, other_model_io_count):
        known = [row for row in self.chat_observations if row["usage"] is not None]
        unknown = len(self.chat_observations) - len(known)
        usage = ({key: sum(row["usage"][key] for row in known)
                  for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
                 if known else None)
        return {"accounting_scope": "chat_completions_only",
            "source_accounting": "not_certified_by_this_report", "other_model_io_count": other_model_io_count,
            "native_count": len(self.chat_observations), "observed_attempts_count": len(self.chat_observations),
            "known_native_attempts": len(known), "known_usage": usage, "unknown_attempts": unknown,
            "observation_state": "no_chat_observation" if not self.chat_observations else
                                 "usage_unknown" if unknown else "known_usage",
            "requires_reconciliation": bool(unknown), "format_reanswers": self.chat_schema_retries,
            "observed_attempts": list(self.chat_observations)}

    @staticmethod
    def usage(usage, mode, ceiling, stage):
        if (DeepSeekRequiredClient._usage(usage, mode) != usage
                or any(type(usage[key]) is not int or usage[key] <= 0
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                or usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]
                or usage["completion_tokens"] > ceiling or usage["total_tokens"] > CONTEXT_TOKENS):
            _fail(stage)

    def source_io(self, stage, bundle, registry, archive):
        """Bind journal requests to settled archive attempts and replay parsers."""
        attempts = {row["attempt_id"]: row for row in bundle["attempts"]}
        seen, native = [], []
        for call, request, response in self.io(stage):
            method = call["method"]
            if method in {"model.deepseek.search", "model.deepseek.fetch"}:
                call_id = request["call_id"]
                if call_id in self.call_ids or not re.fullmatch(r"[0-9a-f]{32}", call_id):
                    _fail(stage)
                self.call_ids.add(call_id)
                native.append((call, request, response))
                continue
            if method == "model.deepseek.chat" and stage == "clean.llm_semantic":
                continue
            if method != "intelligence.source.request":
                _fail(stage)
            attempt = attempts[request["attempt_id"]]
            source = registry.get(request["source_id"])
            parameters = attempt["request"]["parameters"]
            if (request != {"schema": "tiger.intelligence-source-request/v1", "job_id": bundle["job_id"],
                    "attempt_id": attempt["attempt_id"], "source_id": attempt["source_id"],
                    "source_version": source.version, "operation": attempt["operation"],
                    "parameters": parameters, "request_sha256": _hash(parameters),
                    "registry_sha256": registry.public_manifest()["sha256"]}
                    or request["attempt_id"] in seen):
                _fail(stage)
            seen.append(request["attempt_id"])
            self.attempt_stages[request["attempt_id"]] = stage
            if "error_ref" in call:
                if attempt["status"] not in {"failed", "unknown"} or "observation" in attempt["result"]:
                    _fail(stage)
                continue
            _same(response, attempt["result"]["observation"], stage)
            raw = archive.read_blob(response["raw_response_ref"])
            if (response["schema"] != "tiger.intelligence-source-response/v1"
                    or response["source_id"] != source.source_id or response["operation"] != request["operation"]
                    or response["request_sha256"] != request["request_sha256"]
                    or type(response["raw_redacted"]) is not bool
                    or type(response["request_count"]) is not int or response["request_count"] < 1
                    or type(response["bytes_received"]) is not int or response["bytes_received"] < 0
                    or (response["normalization_kind"] == "registered_adapter" and
                        not response["raw_redacted"] and response["bytes_received"] < len(raw))
                    or not re.fullmatch(r"[0-9a-f]{64}", response["transport_sha256"])):
                _fail(stage)
            replayed = replay_observation(source, request["operation"], parameters, raw,
                response_url=response["response_url"], headers=response["http_headers"],
                received_at=response["received_at"], normalization_kind=response["normalization_kind"],
                transport_sha256=response["transport_sha256"], request_count=response["request_count"],
                parse_failure=(response["normalized"].get("metadata", {}).get("parse_failure")
                    if request["operation"] == "fetch" else
                    next((hit.get("metadata", {}).get("parse_failure") for hit in response["normalized"].get("hits", [])
                          if hit.get("metadata", {}).get("parse_failure")), None)))
            normalized = replayed.to_dict() if isinstance(replayed, SearchResult) else {
                key: value for key, value in asdict(replayed).items() if key != "raw_bytes"}
            if request["operation"] == "fetch" and response["raw_redacted"]:
                # Sanitized bytes remain replayable but cannot prove an intact
                # original. This downgrade is independent of parser success.
                normalized["completeness"] = "partial"
            _same(response["usage"], response["normalized"]["usage"], stage)
            if response["raw_redacted"] and normalized["usage"].get("state") == "public_unmetered":
                # Removed credential bytes cannot be reconstructed from the
                # sanitized blob. Retain only the bounded, journal-bound wire
                # byte counter; every parsed fact is still compared below.
                original_bytes = response["usage"].get("bytes")
                if type(original_bytes) is not int or not 0 <= original_bytes <= response["bytes_received"]:
                    _fail(stage)
                normalized["usage"]["bytes"] = original_bytes
            if response["normalization_kind"] == "deepseek_bridge" and request["operation"] == "fetch":
                matching = [item for item in native if item[0]["method"] == "model.deepseek.fetch"
                            and item[1]["request"] == {"url": parameters["url"]}]
                if len(matching) != 1 or "error_ref" in matching[0][0]:
                    _fail(stage)
                ncall, nrequest, nproof = matching[0]
                self.native_fetch(nrequest, nproof, parameters["url"], stage)
                self.bind_native_usage(nproof, response, stage)
                # A bound valid success receipt establishes exactly one
                # successful native request. Historical unknown accounting is
                # retained only for its exact old representation.
                successful_usage = {"state": "reported_requests", "successful_requests": 1, "requires_reconciliation": False}
                if response["usage"] != normalized["usage"]:
                    _same(response["usage"], successful_usage, stage)
                    normalized["usage"] = successful_usage
                _same(strict_json(raw), nproof["page"], stage)
                normalized["metadata"] = {"bridge": "admitted-deepseek-client",
                    "receipt": {key: value for key, value in nproof.items() if key not in {"schema", "page"}},
                    "raw_format": "sanitized_provider_projection", "transport_sha256": hashlib.sha256(raw).hexdigest()}
                native.remove(matching[0])
            _same(safe_value(normalized), response["normalized"], stage)
            if response["normalization_kind"] == "deepseek_bridge" and request["operation"] == "search":
                matching = [item for item in native if item[1]["call_id"] ==
                    response["normalized"].get("response_metadata", {}).get("receipt", {}).get("call_id")]
                if request["operation"] != "search" or len(matching) != 1:
                    _fail(stage)
                ncall, nrequest, nproof = matching[0]
                if "error_ref" in ncall:
                    _fail(stage)
                self.native_search(nrequest, nproof, deepseek_query(_request(parameters)), stage, parameters["count"])
                self.bind_native_usage(nproof, response, stage)
                if response["usage"] != {"state": "unknown", "requires_reconciliation": True}:
                    _same(response["usage"], {"state": "reported_requests", "successful_requests": 1,
                        "requires_reconciliation": False}, stage)
                rows = nproof["results"]
                expected_hits = [_hit(row, url=row.get("url"), title=row.get("title", ""),
                    snippet=row.get("summary", ""), text=row.get("content", ""), text_kind="provider_text",
                    published=row.get("published"), metadata={k: v for k, v in row.items() if k != "content"}) for row in rows]
                _same(response["normalized"]["hits"], expected_hits, stage)
                _same(response["normalized"]["response_metadata"]["receipt"],
                      {key: value for key, value in nproof.items() if key not in {"schema", "results"}}, stage)
                native.remove(matching[0])
        # Bridge failures precede their absent projection. They may explain only
        # a recorded failed/unknown attempt with the exact same compiled query.
        for call, request, response in native:
            if "error_ref" not in call:
                _fail(stage)
            operation = "search" if call["method"] == "model.deepseek.search" else "fetch"
            matches = [row for row in attempts.values() if row["attempt_id"] not in seen
                and row["attempt_id"] not in self.attempt_stages
                and row["source_id"] == "deepseek" and row["operation"] == operation
                and row["status"] in {"failed", "unknown"}
                and (deepseek_query(_request(row["request"]["parameters"])) == request["request"]["query"]
                     if operation == "search" else _source_parameters(registry.get(row["source_id"]), operation,
                         row["request"]["parameters"]) == request["request"])]
            if not matches:
                _fail(stage)
            row = matches[0]
            if operation == "search":
                _verify_search_request(request, request["request"]["query"], stage, row["request"]["parameters"]["count"])
            else:
                self.fetch_request(request, row["request"]["parameters"]["url"], stage)
            if response.get("error_type") != "RequiredModelUnavailable":
                _fail(stage)
            diagnostic = validate_model_diagnostic(response["diagnostic"])
            result = row["result"]
            usage = result["usage"]
            if usage == {"state": "not_charged", "requires_reconciliation": False}:
                if (diagnostic["code"] != "HTTP_ERROR"
                        or urlsplit(registry.get(row["source_id"]).endpoint).hostname != "api.deepseek.com"
                        or result["error"]["details"].get("http_status") != diagnostic["http_status"]
                        or result["error"]["details"].get("delivery") != "returned"):
                    _fail(stage)
            elif usage == {"state": "not_sent", "requires_reconciliation": False}:
                if result["request_count"] != 0 or result["error"]["details"].get("delivery") != "not_sent":
                    _fail(stage)
            else:
                _same(usage, {"state": "unknown", "requires_reconciliation": True}, stage)
            seen.append(row["attempt_id"])
            self.attempt_stages[row["attempt_id"]] = stage
        return seen

    def native_search(self, request, proof, query, stage, count):
        _verify_search_request(request, query, stage, count)
        if (proof["call_id"] != request["call_id"] or proof["stage"] != stage
                or proof["provider"] != "deepseek" or proof["request_sha256"] != request["request_sha256"]
                or not re.fullmatch(r"[0-9a-f]{64}", proof["transport_response_sha256"])):
            _fail(stage)
        _verify_search_rows(proof, request, count)
        self.native_searches.append(proof)

    @staticmethod
    def fetch_request(request, url, stage):
        if (request["schema"] != "tiger.deepseek-fetch-request.v1" or request["stage"] != stage
                or request["provider"] != "deepseek" or request["method"] != "POST"
                or request["endpoint"] != "https://api.deepseek.com/v1/tools/fetch"
                or request["search_mode"] != NATIVE_SEARCH_MODE or request["request"] != {"url": url}
                or request["request_sha256"] != _hash({"url": url}) or sanitize_url(url) != url):
            _fail(stage)

    def native_fetch(self, request, proof, url, stage):
        self.fetch_request(request, url, stage)
        page = proof["page"]
        identity = {key: page[key] for key in ("url", "title", "content", "published")}
        if (proof["schema"] != "tiger.deepseek-fetch.v1" or proof["call_id"] != request["call_id"]
                or proof["stage"] != stage or proof["provider"] != "deepseek" or proof["method"] != "POST"
                or proof["search_mode"] != NATIVE_SEARCH_MODE or proof["request_sha256"] != request["request_sha256"]
                or proof["response_sha256"] != _hash({"page": page})
                or not re.fullmatch(r"[0-9a-f]{64}", proof["transport_response_sha256"])
                or page["url"] != url or not 1 <= len(page["title"]) <= 500 or not page["content"].strip()
                or page["source"] != "deepseek_fetch" or page["id"] != "ds_" + _hash(identity)[:24]
                or page["content_hash"] != hashlib.sha256(page["content"].encode()).hexdigest()):
            _fail(stage)
        self.native_fetches.append(proof)

    @staticmethod
    def bind_native_usage(proof, observation, stage):
        usage = proof.get("transport_usage")
        if usage is None:
            return
        if (set(usage) != {"bytes_received", "request_count"}
                or type(usage["bytes_received"]) is not int or usage["bytes_received"] < 0
                or type(usage["request_count"]) is not int or usage["request_count"] < 1
                or usage["bytes_received"] != observation["bytes_received"]
                or usage["request_count"] != observation["request_count"]):
            _fail(stage)


def _phase_documents(archive, job_id, revision):
    events = archive.events(job_id)
    if type(revision) is not int or not 1 <= revision <= len(events):
        _fail()
    links, seen_hits = {}, set()
    for event in events[:revision]:
        if event["kind"] != "query_hit":
            continue
        hit = event["payload"]
        if hit["hit_id"] in seen_hits:
            continue
        seen_hits.add(hit["hit_id"])
        row = links.setdefault(hit["document_id"], {"targets": set(), "entities": set(), "hits": []})
        row["targets"].update(hit["targets"])
        row["entities"].update(hit["entities"])
        row["hits"].append(hit["hit_id"])
    return [{**archive.document(key), "target_ids": sorted(row["targets"]),
             "requested_entity_ids": sorted(row["entities"]), "query_hit_ids": row["hits"]}
            for key, row in links.items()]


def _entity_registry(plan, documents):
    result = {"securities": plan["universe"], "entities": {}}
    for identity, entity in plan["entity_registry"].items():
        ids = {}
        for row in entity["identifiers"]:
            ids.setdefault(row["scheme"], []).append(row["value"])
        result["entities"][identity] = {**entity, "identifiers": ids}
    directory = next((doc for doc in reversed(documents) if doc["text_kind"] == "identity_directory"), None)
    if directory:
        for ticker, row in directory["source_metadata"].get("identity_directory", {}).items():
            if ticker in plan["universe"]:
                result["entities"]["issuer:" + row["cik"]] = {"type": "issuer", "name": row["name"],
                    "aliases": [row["name"], ticker], "identifiers": {"ticker": [ticker], "cik": [row["cik"]]},
                    "security_ids": [plan["universe"][ticker]], "source_ref": directory["id"]}
    return result


def _normalized_assessment(assessment):
    return {"claims": assessment["claims"], "coverage": [{**row,
        "status": {"assessed": "present", "insufficient_evidence": "missing", "conflicted": "conflicting"}.get(row["status"], row["status"])}
        for row in assessment["coverage"]]}


def _project_gaps(events):
    """Independently fold source diagnostics without accepting saved summaries."""
    latest, diagnostics, seen = {}, {}, {}
    for event in events:
        if event["kind"] != "gap":
            continue
        raw = event["payload"]
        row = {key: value for key, value in raw.items() if key not in {"source_diagnostics", "source_reason"}}
        cell = row["cell_id"]
        latest[cell] = row
        if row.get("diagnostic_kind") == "source":
            detail = {"reason": row["reason"], "status": row["status"],
                "attempt_refs": row.get("attempt_refs", []), "document_ids": row.get("document_ids", [])}
            identity = _hash(detail)
            if identity not in seen.setdefault(cell, set()):
                seen[cell].add(identity)
                diagnostics.setdefault(cell, []).append({**detail, "event_sequence": event["sequence"]})
    return [{**row, **({"source_diagnostics": diagnostics[cell],
                        "source_reason": diagnostics[cell][-1]["reason"]} if cell in diagnostics else {})}
            for cell, row in latest.items()]


def _verify_baseline(packet, bundle):
    baseline = packet["baseline"]
    if baseline is None:
        if bundle["baseline_refs"]:
            _fail("intelligence.freeze")
        return
    manifest = baseline["manifest"]
    validate_baseline_manifest(manifest)
    scoped = manifest["schema"] == "topic_snapshot/v1"
    if manifest["as_of"] != bundle["as_of"] or len(bundle["baseline_refs"]) != 1:
        _fail("intelligence.freeze")
    selected_ids = {row["id"] for row in manifest["versions"]}
    if scoped:
        # Recompute membership independently of the runtime selection helper.
        # Only labels in this admitted artifact can nominate a raw original;
        # those labels never establish semantic scoring/action eligibility.
        entities = set(bundle["allowed_entities"])
        targets = {row["target_id"] for row in bundle["gaps"] if not row["target_id"].startswith("security:")}
        targets.update(value.split(":", 1)[-1] for value in tuple(targets)
            if value.startswith(("chain:", "sector:", "topic:")))
        selected_ids = set()
        for member in manifest["members"]:
            subjects = {row if isinstance(row, str) else row.get("entity_id", row.get("id"))
                for row in member["validated_subjects"] if isinstance(row, (str, dict))}
            if (entities.intersection(member["requested_entity_ids"]) or entities.intersection(subjects)
                    or targets.intersection(member["topic_ids"])):
                selected_ids.add(member["id"])
    selected_versions = [row for row in manifest["versions"] if row["id"] in selected_ids]
    receipt = bundle["baseline_refs"][0]
    expected = {"archive_id": manifest["archive_id"], "manifest_sha256": manifest["manifest_sha256"],
        "manifest": manifest, "event_head": manifest["fixed_event_frontier"] if scoped else manifest["event_head"],
        "versions": selected_versions,
        "imported_at": receipt["imported_at"], "reused_semantic_annotations": False, "reused_search_results": False}
    if scoped:
        expected["snapshot_id"] = manifest["snapshot_id"]
        if manifest["registry_hash"] not in {"", packet["registry"]["sha256"]}:
            _fail("intelligence.freeze")
        head = manifest["fixed_event_frontier"]
        cutoff = datetime.fromisoformat(manifest["cutoff"])
        for member in manifest["members"]:
            creation = member["creation_event"]
            provenance = [creation, *(row["event"] for row in member["membership_provenance"])]
            for event in provenance:
                value = {key: value for key, value in event.items() if key not in {"sequence", "sha256"}}
                if (event["tenant"] != manifest["tenant"] or event["sha256"] != _hash(value)
                        or event["sequence"] > head["sequence"] or datetime.fromisoformat(event["at"]) > cutoff
                        or event["payload"].get("document_id") != member["id"]):
                    _fail("intelligence.freeze")
            if (creation["kind"] != "document_version" or creation["payload"].get("document_sha256") != member["document_sha256"]
                    or datetime.fromisoformat(member["first_seen_at"]) > cutoff):
                _fail("intelligence.freeze")
            requested = set()
            topics = set()
            for association in member["membership_provenance"]:
                event = association["event"]
                kind = association["kind"]
                if kind == "requested_entity":
                    if event["kind"] != "query_hit" or association["value"] not in event["payload"]["entities"]:
                        _fail("intelligence.freeze")
                    requested.add(association["value"])
                elif kind in {"topic", "target"}:
                    targets = event["payload"].get("targets", [])
                    if (event["kind"] != "query_hit" or association["value"] not in targets
                            and not any(value.split(":", 1)[-1] == association["value"] for value in targets)):
                        _fail("intelligence.freeze")
                    topics.add(association["value"])
                elif kind == "receipt":
                    if event["kind"] != "query_hit" or association["value"] != str(event["sequence"]):
                        _fail("intelligence.freeze")
                elif kind in {"source", "material", "validated_subject"}:
                    subject_ids = {row if isinstance(row, str) else row.get("entity_id", row.get("id"))
                        for row in member["validated_subjects"] if isinstance(row, (str, dict))}
                    expected_values = {member["source_id"]} if kind == "source" else {member["material_type"]} if kind == "material" else subject_ids
                    if event != creation or association["value"] not in expected_values:
                        _fail("intelligence.freeze")
                else:
                    _fail("intelligence.freeze")
            if member["requested_entity_ids"] != sorted(requested) or member["topic_ids"] != sorted(topics):
                _fail("intelligence.freeze")
    _same(receipt, expected, "intelligence.freeze")
    by_id = {doc["id"]: doc for doc in bundle["documents"]}
    members = {member["id"]: member for member in manifest.get("members", [])}
    for version in selected_versions:
        document = by_id[version["id"]]
        original = {key: value for key, value in document.items()
                    if key not in {"target_ids", "requested_entity_ids", "query_hit_ids"}}
        if (version["doc_id"] != document["doc_id"] or version["version"] != document["version"]
                or version["text_sha256"] != document["text_blob"]["sha256"]
                or version["raw_sha256"] != document["raw_blob"]["sha256"]
                or version["document_sha256"] != _hash(original)
                or version["first_seen_at"] != document["times"]["first_seen"]["value"]
                or datetime.fromisoformat(version["first_seen_at"]) > datetime.fromisoformat(bundle["as_of"])):
            _fail("intelligence.freeze")
        if scoped:
            member = members[version["id"]]
            for key in ("source_id", "source_version", "raw_blob", "text_blob"):
                _same(member[key], original[key], "intelligence.freeze")
            _same(member["validated_subjects"], original.get("validated_subjects", []), "intelligence.freeze")


def _verify_document_attempts(bundle, packet):
    attempts = {row["attempt_id"]: row for row in bundle["attempts"]}
    registry = default_registry(packet["config"])
    baseline_ids = {row["id"] for receipt in bundle["baseline_refs"] for row in receipt["versions"]}
    def bind(recipe, attempt, *, discovery=False):
        observation = attempt["result"].get("observation")
        if observation is None or attempt["status"] not in {"completed", "empty"}:
            _fail()
        comparisons = {"parameters": attempt["request"]["parameters"], "raw_response_ref": observation["raw_response_ref"],
            "response_url": observation["response_url"], "http_headers": observation["http_headers"],
            "received_at": observation["received_at"], "transport_sha256": observation["transport_sha256"],
            "request_count": observation["request_count"], "raw_redacted": observation["raw_redacted"]}
        for key, value in comparisons.items():
            if recipe.get(key, False if key == "raw_redacted" else None) != value:
                _fail()
        if "actual_request" in recipe and recipe["actual_request"] != observation["actual_request"]:
            _fail()
        kind = recipe.get("normalization_kind" if discovery else "kind")
        if (recipe["source_id"] != attempt["source_id"] or kind != observation["normalization_kind"]
                or attempt["operation"] != ("search" if discovery else "fetch")):
            _fail()
    for document in bundle["documents"]:
        recipe = document["normalization"]
        seen_at = datetime.fromisoformat(document["times"]["first_seen"]["value"])
        received_at = datetime.fromisoformat(recipe["received_at"])
        frozen_at = datetime.fromisoformat(bundle["frozen_at"])
        if (seen_at.utcoffset() is None or received_at.utcoffset() is None or frozen_at.utcoffset() is None
                or not received_at <= seen_at <= frozen_at
                or document["times"]["first_seen"] != source_time(seen_at.isoformat(), evidence={"kind": "local_observation"})
                or document["times"]["retrieved"] != source_time(recipe["received_at"], evidence={"kind": "source_transport"})):
            _fail()
        if document["id"] in baseline_ids:
            continue
        refs = document["source_attempt_refs"]
        if not isinstance(refs, list) or len(set(refs)) != len(refs) or len(refs) != (2 if recipe["operation"] == "fetch" else 1):
            _fail()
        discovery = attempts[refs[0]]
        bind(recipe["discovery"], discovery, discovery=True)
        hits = discovery["result"]["observation"]["normalized"]["hits"]
        index = recipe["hit_index"]
        if (type(index) is not int or not 0 <= index < len(hits)
                or recipe["discovery"]["hit_index"] != index):
            _fail()
        if recipe["operation"] == "fetch":
            bind(recipe, attempts[refs[1]])
            parent = recipe.get("attachment", {}).get("parent_normalization")
            if parent is not None:
                parent_operation = parent.get("operation", "fetch")
                matches = [row for row in bundle["attempts"] if row["operation"] == parent_operation
                    and row["source_id"] == parent["source_id"]
                    and row["request"]["parameters"] == parent["parameters"]
                    and row["result"].get("observation", {}).get("raw_response_ref") == parent["raw_response_ref"]]
                if len(matches) != 1:
                    _fail()
                parent_recipe = {**parent, "normalization_kind": parent["kind"]} if parent_operation == "search" else parent
                bind(parent_recipe, matches[0], discovery=parent_operation == "search")
            elif _source_parameters(registry.get(recipe["source_id"]),
                    "fetch", recipe["parameters"]) != {"url": hits[index]["url"]}:
                _fail()
        else:
            # The search-only document's principal recipe is the same captured
            # discovery response, without an additional fetch authority.
            for key in ("source_id", "parameters", "raw_response_ref", "response_url", "http_headers",
                        "received_at", "transport_sha256", "request_count", "raw_redacted"):
                if recipe.get(key, False if key == "raw_redacted" else None) != recipe["discovery"].get(
                        key, False if key == "raw_redacted" else None):
                    _fail()


def _verify_attempt_plan(replay, registry, bundle, plan, initial_documents, rounds):
    """Recompile admitted questions; arbitrary journal URLs cannot create a plan."""
    if plan.get("acquisition_mode") == "snapshot_only":
        if plan["query_plan"] or bundle["attempts"]:
            _fail()
        return
    directory = next((doc for doc in reversed(initial_documents) if doc["text_kind"] == "identity_directory"), None)
    allowed = {"search.collect": set(), "clean.llm_semantic": set()}
    for entry in plan["query_plan"]:
        request = _request(entry["request"])
        request = replace(request, context={**request.context, "plan_request_hash": _hash(entry["request"])})
        if registry.get(entry["source_id"]).kind == "sec_submissions" and directory:
            row = directory["source_metadata"].get("identity_directory", {}).get(request.context.get("ticker"))
            if row:
                request = replace(request, context={**request.context, "cik": row["cik"],
                    "issuer_directory_ref": {"document_id": directory["id"],
                        "content_sha256": directory["text_blob"]["sha256"], "raw_response_ref": directory["raw_blob"],
                        "ticker": request.context["ticker"], "cik": row["cik"],
                        "first_seen_at": directory["times"]["first_seen"]["value"]}})
        for compiled in compile_requests(entry["source_id"], request):
            allowed["search.collect"].add((entry["source_id"], _hash(compiled.to_dict())))
    # SEC explicitly paginates an issuer's older submissions files. A child
    # request is admitted only by a raw parent index that was itself planned.
    for attempt in bundle["attempts"]:
        source = attempt["source_id"]
        parameters = attempt["request"]["parameters"]
        if (attempt["operation"] != "search" or registry.get(source).kind != "sec_submissions"
                or not parameters.get("context", {}).get("submissions_file")):
            continue
        context = parameters["context"]
        base = {**parameters, "context": {key: value for key, value in context.items()
                                          if key not in {"issuer_name", "submissions_file"}}}
        if (source, _hash(base)) not in allowed["search.collect"]:
            _fail()
        parent_attempts = [row for row in bundle["attempts"] if row["source_id"] == source
            and row["operation"] == "search" and row["request"]["parameters"] == base
            and "observation" in row["result"]]
        matched = False
        for parent_attempt in parent_attempts:
            observation = parent_attempt["result"]["observation"]
            metadata = observation["normalized"]["response_metadata"]
            for older in metadata.get("older_files", []):
                name = older.get("name")
                cik = str(base["context"].get("cik", "")).zfill(10)
                if (not isinstance(name, str) or re.fullmatch(r"CIK" + cik + r"-submissions-\d{3,}\.json", name) is None
                        or base["start_date"] and older.get("filingTo", "9999") < base["start_date"]
                        or base["end_date"] and older.get("filingFrom", "0000") > base["end_date"]):
                    continue
                parent = {"raw_response_ref": observation["raw_response_ref"], "response_url": observation["response_url"],
                    "transport_sha256": observation["transport_sha256"], "cik": cik, "name": name,
                    "issuer_name": metadata.get("response", {}).get("name", "")}
                expected = {**base, "context": {**base["context"], "issuer_name": parent["issuer_name"],
                                                "submissions_file": {"name": name, "parent_ref": parent}}}
                if parameters == expected:
                    matched = True
        if not matched:
            _fail()
        allowed["search.collect"].add((source, _hash(parameters)))
    providers = [source for source in plan["source_ids"] if registry.get(source).kind == "search"]
    # At most one targeted research pass is admitted by TOOL_LIMITS.rounds=2.
    if rounds and providers:
        provider = providers[min(1, len(providers) - 1)]
        cells = {cell["cell_id"]: cell for cell in plan["coverage_cells"]}
        for row in rounds[0]["assessment"]["coverage"]:
            cell = cells[row["cell_id"]]
            if not cell["required"] or row["status"] == "present" or cell["material_type"] == "connection":
                continue
            request = SearchRequest(cell["query"] + " primary source correction withdrawal contrary evidence", count=10,
                start_date=(replay.cutoff.date() - timedelta(days=cell["window_days"])).isoformat(),
                end_date=replay.cutoff.date().isoformat(), topic="news", targets=(cell["target_id"],),
                entities=(cell["entity_id"],) if cell["entity_id"] else (), context={"research_round": 2})
            for compiled in compile_requests(provider, request):
                allowed["clean.llm_semantic"].add((provider, _hash(compiled.to_dict())))
    fetches = set()
    for attempt in bundle["attempts"]:
        if attempt["operation"] != "search" or "observation" not in attempt["result"]:
            continue
        source = attempt["source_id"]
        spec = registry.get(source)
        fetch_source = (source if spec.kind not in {"search", "web_fetch"} else
                        "web_page" if "web_page" in plan["source_ids"] else source)
        for hit in attempt["result"]["observation"]["normalized"]["hits"]:
            fetches.add((fetch_source, hit["url"]))
            if spec.kind == "page":
                fetches.update((fetch_source, attachment["url"]) for attachment in hit.get("attachments", []))
    for attempt in bundle["attempts"]:
        if attempt["operation"] == "fetch" and "observation" in attempt["result"]:
            for attachment in attempt["result"]["observation"]["normalized"].get("attachments", []):
                fetches.add((attempt["source_id"], attachment["url"]))
    for attempt in bundle["attempts"]:
        source = attempt["source_id"]
        parameters = _source_parameters(registry.get(source), attempt["operation"], attempt["request"]["parameters"])
        if source not in plan["source_ids"] or attempt["request_sha256"] != _hash(attempt["request"]):
            _fail()
        stage = replay.attempt_stages.get(attempt["attempt_id"])
        if attempt["operation"] == "search":
            if stage is None:
                possible = any((source, _hash(parameters)) in options for options in allowed.values())
            else:
                possible = (source, _hash(parameters)) in allowed[stage]
            if not possible or not 0 <= attempt["ordinal"] <= registry.get(source).max_retries:
                _fail(stage)
        elif attempt["operation"] != "fetch" or set(parameters) != {"url"} or (source, parameters["url"]) not in fetches:
            _fail(stage)


def _verify_phase_status(archive, snapshot, bundle, plan):
    status = snapshot["status"]
    events = archive.events(bundle["job_id"])[:status["revision"]]
    phase_ids = {event["payload"]["attempt_id"] for event in events if event["kind"] == "attempt_started"}
    attempts = [row for row in bundle["attempts"] if row["attempt_id"] in phase_ids]
    state = "accepted"
    for event in events:
        if event["kind"] == "job_state":
            state = event["payload"]["state"]
    usage = {"requests": sum(row["result"]["request_count"] for row in attempts),
        "fetches": sum(row["operation"] == "fetch" for row in attempts),
        "bytes": sum(row["result"]["bytes_received"] for row in attempts),
        "unreconciled_attempts": sum(bool(row["result"]["usage"].get("requires_reconciliation")) for row in attempts)}
    expected = {"job_id": bundle["job_id"], "run_id": bundle["run_id"], "state": state,
        "revision": len(events), "created_at": archive.status(bundle["job_id"])["created_at"],
        "plan_hash": plan["plan_hash"], "gaps": _project_gaps(events), "attempts": attempts,
        "inflight_attempts": [], "usage": usage, "effective_concurrency": 1, "bundle": None}
    _same(status, expected)
    created = datetime.fromisoformat(expected["created_at"])
    if (events[0]["kind"] != "job_created" or created.utcoffset() is None
            or created > datetime.fromisoformat(events[0]["at"])):
        _fail()
    for attempt in attempts:
        started = datetime.fromisoformat(attempt["started_at"])
        if (started < created or started > created + timedelta(seconds=plan["limits"]["deadline_seconds"])
                or datetime.fromisoformat(attempt["completed_at"]) < started):
            _fail()


def _verify_acquisition(replay):
    packet = validate_input_packet(read_json(replay.run, "intelligence-input.json"))
    mode = acquisition_mode(packet["config"])
    if mode == "snapshot_only":
        for stage, output in replay.outputs.items():
            if any(call["method"] in {"model.deepseek.search", "model.deepseek.fetch", "intelligence.source.request"}
                   for call in output["io_calls"]):
                _fail(stage)
    if replay.policy is not None:
        _same(replay.policy["intelligence"], {**acquisition_policy(), "input_sha256": value_hash(packet)})
        for key, expected in {"source_content_contract": SOURCE_CONTENT_CONTRACT,
                "company_evidence_contract": semantic.COMPANY_CONTRACT, "event_contract": STRUCTURED_EVENT_CONTRACT,
                "risk_evidence_contract": RISK_CONTRACT, "search_mode": SEARCH_MODE}.items():
            if replay.policy["model_contract"].get(key) != expected:
                _fail()
    registry = default_registry(packet["config"])
    frontier = replay.value("intelligence.frontier")
    prepared = replay.value("pipeline.prepare")
    market_output = replay.value("data.prepare")
    market = market_output["market_data"]
    numeric = {"spy": restore_frame(market["spy"]), "stock_ohlcv": {
        key: restore_frame(value) for key, value in market["stock_ohlcv"].items()}}
    finput = replay.inputs["intelligence.frontier"]
    _same(finput["universe"], market_output["universe"], "intelligence.frontier")
    _same(finput["portfolio_state"], replay.inputs["pipeline.prepare"]["portfolio_state"], "intelligence.frontier")
    _same(finput["market_snapshot_ref"], replay.ends["data.prepare"]["output_ref"], "intelligence.frontier")
    from ..markets import get_market
    spec = get_market("us")
    verify_frontier(frontier, numeric, finput["universe"], as_of=replay.cutoff,
        market_snapshot_ref=replay.ends["data.prepare"]["output_ref"],
        portfolio_state=finput["portfolio_state"], scan_filters=spec.scan_filters(), sector_symbols=spec.sector_symbols)
    _same(frontier["security_ids"], prepared["security_roster"]["symbols"], "intelligence.frontier")
    plan = build_plan({"run_id": replay.run.name, "as_of": replay.cutoff.isoformat(), "market": "us",
        "universe": frontier["security_ids"], "entities": entity_registry(frontier)["entities"],
        "frontier": frontier["symbols"], "frontier_manifest_hash": frontier["manifest_sha256"],
        "market_snapshot_refs": frontier["market_snapshot_refs"], "holdings": frontier["holdings"], "pending": frontier["pending"],
        "watchlist": frontier["watchlist"], "topics": semantic.acquisition_topics({**CHAIN_TOPICS, **SECTOR_TOPICS}),
        "baseline_refs": ([{"manifest_sha256": packet["baseline"]["manifest"]["manifest_sha256"]}]
                          if packet["baseline"] else []), "limits": dict(TOOL_LIMITS),
        **({"acquisition_mode": "snapshot_only"} if mode == "snapshot_only" else {})}, registry)
    freeze = replay.value("intelligence.freeze")
    archive_root = replay.run / "intelligence"
    checked_root(archive_root)
    safe_path(archive_root, "intelligence.sqlite3")
    for suffix in ("-wal", "-shm"):
        candidate = archive_root / ("intelligence.sqlite3" + suffix)
        if candidate.exists() or candidate.is_symlink():
            safe_path(archive_root, candidate.name)
    safe_path(archive_root, "bundles/" + freeze["bundle_id"] + ".json")
    def inspect_refs(value):
        if isinstance(value, dict):
            if {"path", "sha256", "bytes"} <= value.keys() and str(value["path"]).startswith("blobs/"):
                safe_path(archive_root, value["path"])
            for item in value.values():
                inspect_refs(item)
        elif isinstance(value, list):
            for item in value:
                inspect_refs(item)
    inspect_refs(freeze)
    archive = Archive(archive_root, readonly=True)
    bundle = verify_bundle(archive_root, freeze["bundle_id"], expected_plan_hash=plan["plan_hash"],
        expected_registry_sha256=registry.public_manifest()["sha256"])
    _same(freeze, bundle, "intelligence.freeze")
    _same(archive.plan(bundle["job_id"]), plan, "intelligence.freeze")
    _verify_baseline(packet, bundle)
    expected_job = "intel_" + _hash({"tenant": "public", "replay_key": "daily:" + replay.run.name,
                                     "plan_hash": plan["plan_hash"]})[:32]
    if (bundle["job_id"] != expected_job or bundle["run_id"] != replay.run.name
            or bundle["as_of"] != replay.cutoff.isoformat()
            or bundle["frontier_manifest_hash"] != frontier["manifest_sha256"]
            or bundle["market_snapshot_refs"] != frontier["market_snapshot_refs"]
            or bundle["allowed_entities"] != plan["allowed_entities"]):
        _fail("intelligence.freeze")
    observed = []
    for stage in ("search.collect", "clean.llm_semantic"):
        observed.extend(replay.source_io(stage, bundle, registry, archive))
    attempts = bundle["attempts"]
    # A locally denied request before observe_io can remain a settled not-sent
    # attempt; it is never allowed to contribute a source document.
    absent = [row for row in attempts if row["attempt_id"] not in observed]
    if (len(observed) != len(set(observed)) or any(row["status"] not in {"failed", "unknown"}
            or row["result"].get("request_count") != 0
            or row["result"].get("usage", {}).get("state") != "not_sent" for row in absent)):
        _fail()
    _verify_document_attempts(bundle, packet)
    usage = {"requests": sum(row["result"]["request_count"] for row in attempts),
        "fetches": sum(row["operation"] == "fetch" for row in attempts),
        "bytes": sum(row["result"]["bytes_received"] for row in attempts),
        "unreconciled_attempts": sum(bool(row["result"]["usage"].get("requires_reconciliation")) for row in attempts)}
    _same(bundle["usage"], usage)
    if any(usage[key] > plan["limits"][key] for key in ("requests", "fetches", "bytes")):
        _fail()
    snapshots = [replay.value(stage)["acquisition"] for stage in ("search.collect", "clean.llm_semantic")]
    phases = []
    for stage, snapshot in zip(("search.collect", "clean.llm_semantic"), snapshots, strict=True):
        if (set(snapshot) != {"contract", "archive_relative", "plan", "job_id", "entity_registry", "rounds", "status"}
                or snapshot["contract"] != ACQUISITION_CONTRACT or snapshot["archive_relative"] != "intelligence"
                or snapshot["job_id"] != expected_job or snapshot["plan"] != plan):
            _fail(stage)
        documents = _phase_documents(archive, expected_job, snapshot["status"]["revision"])
        _same(snapshot["entity_registry"], _entity_registry(plan, documents), stage)
        _verify_phase_status(archive, snapshot, bundle, plan)
        phases.append(documents)
    _verify_attempt_plan(replay, registry, bundle, plan, phases[0], snapshots[1]["rounds"])
    if mode == "snapshot_only":
        _verify_snapshot_only(archive, bundle, plan, snapshots, phases)
    initial = project_to_rawdocuments(phases[0])
    collection = replay.value("search.collect")
    _same(collection, {"documents": initial, "topics": {**CHAIN_TOPICS, **SECTOR_TOPICS}, "batches": [],
        "cleanup": {"native_calls_completed": sum(row["stage"] == "search.collect" for row in replay.native_searches)},
        "acquisition": snapshots[0]}, "search.collect")
    if snapshots[0]["rounds"]:
        _fail("search.collect")
    base = rule_base_clean(initial, preserve_source=True)
    _same(replay.inputs["clean.rule_base"], {"documents": initial,
        "source_content_contract": SOURCE_CONTENT_CONTRACT}, "clean.rule_base")
    _same(replay.value("clean.rule_base"), {"documents": base}, "clean.rule_base")
    _same(replay.inputs["clean.llm_semantic"], {"documents": base, "assessment": replay.assessment}, "clean.llm_semantic")
    return registry, archive, bundle, frontier, plan, snapshots, phases, market


def _verify_snapshot_only(archive, bundle, plan, snapshots, phases):
    """Rebuild the no-dispatch record from imported raw versions and events."""
    expected_usage = {"requests": 0, "fetches": 0, "bytes": 0, "unreconciled_attempts": 0}
    imported = {version["id"] for receipt in bundle["baseline_refs"] for version in receipt["versions"]}
    if (not imported or bundle["attempts"] or bundle["usage"] != expected_usage
            or any({document["id"] for document in phase} != imported for phase in phases)):
        _fail("intelligence.freeze")
    events = [event for event in archive.events(bundle["job_id"])
              if event["sequence"] <= bundle["event_head"]["sequence"]]
    collections = [event for event in events if event["kind"] == "snapshot_collection_completed"]
    if len(collections) != 1:
        _fail("search.collect")
    collection = collections[0]
    payload = collection["payload"]
    if (payload.get("state") not in {"partial", "budget_exhausted"}
            or collection["sequence"] > snapshots[0]["status"]["revision"]):
        _fail("search.collect")
    _same(payload, {"mode": "snapshot_only", "document_ids": sorted(imported),
        "documents_sha256": _hash(phases[0]), "state": payload["state"], "request_count": 0, "bytes_received": 0}, "search.collect")
    starts = [event for event in events if event["kind"] == "research_started"]
    unavailable = [event for event in events if event["kind"] == "research_unavailable"]
    completions = [event for event in events if event["kind"] == "research_completed"]
    if not len(starts) == len(unavailable) == len(completions) <= plan["limits"]["rounds"] - 1:
        _fail("clean.llm_semantic")
    cells = {cell["cell_id"]: cell for cell in plan["coverage_cells"]}
    for index, (start, denied, complete) in enumerate(zip(starts, unavailable, completions, strict=True)):
        number = index + 2
        before = _project_gaps(events[:start["sequence"] - 1])
        requested = [gap["cell_id"] for gap in before if gap["required"] and gap["status"] != "present"]
        gaps = [gap for gap in before if gap["cell_id"] in requested]
        _same(start["payload"], {"round": number, "gap_ids": requested, "gaps": gaps,
            "replay_key": None, "requested_gap_ids": requested, "query_plan": [], "mode": "snapshot_only"}, "clean.llm_semantic")
        if not collection["sequence"] < start["sequence"] < denied["sequence"] < complete["sequence"]:
            _fail("clean.llm_semantic")
        expired = complete["payload"].get("state") == "budget_exhausted"
        _same(complete["payload"], {"round": number, "state": "budget_exhausted" if expired else "unavailable",
            "mode": "snapshot_only"}, "clean.llm_semantic")
        _same(denied["payload"], {"round": number, "mode": "snapshot_only",
            "reason": "BUDGET_EXHAUSTED" if expired else "SNAPSHOT_ONLY_NO_LIVE_ACQUISITION",
            "code": "BUDGET_EXHAUSTED" if expired else "POLICY_DENIED",
            "gap_ids": [gap["cell_id"] for gap in gaps if cells[gap["cell_id"]]["material_type"] != "connection"],
            "request_count": 0, "bytes_received": 0}, "clean.llm_semantic")


def _verify_clean(replay, registry, archive, bundle, frontier, plan, snapshots, phases):
    stage = "clean.llm_semantic"
    calls = [item for item in replay.io(stage) if item[0]["method"] == "model.deepseek.chat"]
    rounds = snapshots[1]["rounds"]
    if not 1 <= len(rounds) <= TOOL_LIMITS["rounds"]:
        _fail(stage)
    cleaned, seen, index = [], set(), 0
    # The first phase and any gap-research phase are independently reconstructed
    # from query_hit records, not accepted from the model's document ID list.
    for number, round_record in enumerate(rounds):
        current = project_to_rawdocuments(phases[0 if number == 0 else 1])
        fresh = [doc for doc in rule_base_clean(current, preserve_source=True) if doc.doc_id not in seen]
        if number and not fresh:
            _fail(stage)
        batches = semantic.plan_clean_documents(fresh, replay.planner(stage))
        _same(round_record["document_ids"], [doc.doc_id for doc in fresh], stage)
        _same(round_record["model_batches"], [[doc.doc_id for doc in batch] for batch, _, _ in batches], stage)
        if round_record["round"] != number:
            _fail(stage)
        for batch, payload, schema in batches:
            call, request, proof = calls[index]
            index += 1
            output = replay.chat(stage, call, request, proof, payload, schema)
            rows = output["documents"]
            if len(rows) != len(batch) or {row["id"] for row in rows} != {doc.doc_id for doc in batch}:
                _fail(stage)
            annotations = {row["id"]: row for row in rows}
            for raw in batch:
                cleaned.append(StructuredDocument(raw=raw, evidence=evidence_for(raw),
                    **{key: value for key, value in annotations[raw.doc_id].items() if key != "id"}))
                seen.add(raw.doc_id)
        stats = validate_structured_events(cleaned, replay.cutoff, security_ids=frontier["security_ids"],
            entity_registry=snapshots[1]["entity_registry"], source_policies=registry.public_manifest())
        _same(round_record["status"], stats, stage)
        _same(round_record["assessment"], semantic.assessment_rows(cleaned, plan), stage)
    if index != len(calls):
        _fail(stage)
    _same(replay.value(stage), {"documents": cleaned, "degraded_count": 0, "acquisition": snapshots[1]}, stage)
    _same(replay.inputs["clean.cross_validate"], {"documents": cleaned,
        "event_contract": STRUCTURED_EVENT_CONTRACT, "security_ids": frontier["security_ids"]}, "clean.cross_validate")
    stats = validate_structured_events(cleaned, replay.cutoff, security_ids=frontier["security_ids"],
        entity_registry=snapshots[1]["entity_registry"], source_policies=registry.public_manifest())
    scored = scoring_docs(cleaned)
    _same(replay.value("clean.cross_validate"), {"documents": cleaned, "scoring_documents": scored, "stats": stats}, "clean.cross_validate")
    _same(replay.inputs["intelligence.freeze"], {"frontier_hash": frontier["manifest_sha256"],
        "documents": cleaned, "stats": stats}, "intelligence.freeze")
    assessment = _normalized_assessment(semantic.assessment_rows(cleaned, plan))
    _same(bundle["claims"], assessment["claims"], "intelligence.freeze")
    _same(bundle["coverage"], assessment["coverage"], "intelligence.freeze")
    gaps = [{**row, "reason": row.get("reason", "DETERMINISTIC_ASSESSMENT"),
             "required": cell["required"]} for row, cell in zip(assessment["coverage"], plan["coverage_cells"], strict=True)]
    observed_gaps = [{key: value for key, value in row.items() if key not in {"source_diagnostics", "source_reason"}}
                     for row in bundle["gaps"]]
    _same(observed_gaps, gaps, "intelligence.freeze")
    frozen_events = [event for event in archive.events(bundle["job_id"])
                     if event["sequence"] <= bundle["event_head"]["sequence"]]
    _same(bundle["gaps"], _project_gaps(frozen_events), "intelligence.freeze")
    missing = [row["cell_id"] for row in gaps if row["required"] and row["status"] != "present"]
    _same(bundle["missing_required"], missing, "intelligence.freeze")
    if bundle["state"] != ("partial" if missing else "completed"):
        _fail("intelligence.freeze")
    return cleaned, scored, stats


def _analysis(replay, stage, docs, targets):
    sources = target_evidence_sources(docs, targets)
    coverage = target_evidence_coverage(docs, targets)
    schema_fn = {"tech.sentiment": sentiment_schema, "tech.risk": risk_schema,
                 "sector.narrative": narrative_schema}[stage]
    schema = schema_fn(targets, [doc.raw.doc_id for doc in docs], allow_empty=True,
                       target_sources=sources, target_coverage=coverage)
    inputs = {"etfs" if stage == "sector.narrative" else "chains": targets,
              "target_sources": sources, "target_coverage": coverage}
    payload = semantic.plan_analysis_documents(docs, inputs, schema, replay.planner(stage))
    calls = list(replay.io(stage))
    if len(calls) != 1:
        _fail(stage)
    output = replay.chat(stage, *calls[0], payload, schema)
    validate_target_assessments(stage, output, targets, sources, target_coverage=coverage)
    return output


def _verify_analysis(replay, scored):
    chains = list(TECH_SUBCHAINS)
    expected_input = {"documents": scored, "assessment": replay.assessment}
    _same(replay.inputs["tech.sentiment"], expected_input, "tech.sentiment")
    sent = _analysis(replay, "tech.sentiment", scored, chains)
    sentiments = {row["chain_id"]: StructuredSentimentRow(**row, degraded=row["status"] != "assessed") for row in sent["chains"]}
    _same(replay.value("tech.sentiment"), sentiments, "tech.sentiment")
    _same(replay.inputs["tech.risk"], {**expected_input, "risk_coverage": risk_coverage(scored)}, "tech.risk")
    risk = _analysis(replay, "tech.risk", scored, chains)
    alerts = [RiskAlert(**row) for row in risk["alerts"]]
    _same(replay.value("tech.risk"), alerts, "tech.risk")
    coverage = risk_coverage(scored, assessments=risk["chain_assessments"], alerts=risk["alerts"])
    fusion_input = replay.inputs["tech.fusion"]
    monitor, linkage = replay.value("tech.monitor"), replay.value("tech.cycle_linkage")
    _same(fusion_input, {"monitor": monitor, "linkage": linkage, "sentiment": sentiments,
                         "alerts": alerts, "risk_coverage": coverage}, "tech.fusion")
    monitor_rows = [ChainMonitorRow(**{key: value for key, value in row.items() if key != "links"},
        links=[LinkMomentum(**link) for link in row["links"]]) for row in _plain(monitor)]
    signals = TechChainFusionAgent().execute(monitor_rows, [GlobalLinkageRow(**row) for row in _plain(linkage)],
        sentiments, alerts, risk_evidence_available=bool(scored), risk_coverage_by_chain=coverage["targets"])
    _same(replay.value("tech.fusion"), signals, "tech.fusion")
    signal_map = {signal.main_chain: signal for signal in signals}
    tss, risk_input = replay.inputs["layer3.tss"], replay.inputs["layer4.risk"]
    _same(tss["tech_bonus_map"], signals_to_bonus_map(signals), "layer3.tss")
    _same(tss["tech_signal_map"], signal_map, "layer3.tss")
    _same(risk_input["tech_signal_map"], signal_map, "layer4.risk")
    narrative_input = replay.inputs["sector.narrative"]
    _same(narrative_input, {**expected_input, "sector_symbols": narrative_input["sector_symbols"]}, "sector.narrative")
    from ..markets import get_market
    _same(narrative_input["sector_symbols"], get_market("us").sector_symbols, "sector.narrative")
    out = _analysis(replay, "sector.narrative", scored, narrative_input["sector_symbols"])
    narrative = {row["etf"]: {"score": row["narrative_score"], "bias": row["eps_revision_bias"],
        "tone": row["guidance_tone"], "evidence": "; ".join(row["evidence"]),
        "status": row["status"], "evidence_ids": row["evidence_ids"]} for row in out["sectors"]}
    selected = narrative
    _same(replay.value("sector.narrative"), {"raw_result": narrative, "narrative": selected}, "sector.narrative")
    _same(replay.inputs["layer2.sector"]["narrative"], selected, "layer2.sector")
    return signals


def _verify_decision(replay, cleaned, frontier, bundle, market, result):
    stage = "decision.debate"
    bound = replay.inputs[stage]
    inputs = _plain(bound)
    risk = replay.value("layer4.risk")
    if not _same_watch_facts(bound["watchlist"], replay.value("layer3.tss")["watchlist"]):
        _fail(stage)
    for actual, expected in [(bound["picks"], risk["picks"]),
        (bound["mrs"], replay.value("layer1.mrs")),
        (bound["sectors"], replay.value("layer2.sector")["sectors"]),
        *((bound[key], risk[source]) for key, source in
          (("action", "action"), ("rationale", "pick_rationale"), ("rejections", "rejections")))]:
        _same(actual, expected, stage)
    _same(bound["documents"], cleaned, stage)
    _same(inputs["assessment"], replay.assessment, stage)
    picks = {pick["ticker"]: pick for pick in inputs["picks"]}
    approved = list(picks)
    watch = [row for row in inputs["watchlist"] if row["ticker"] not in picks][:5]
    symbols = approved + [row["ticker"] for row in watch]
    # Entry and watch facts cannot introduce an unresearched late scanner result.
    if not set(symbols) <= set(frontier["symbols"]):
        _fail(stage)
    market_evidence = semantic.market_snapshots(market, symbols, replay.assessment)
    asof = semantic.market_asof(market, symbols, replay.assessment)
    _same(inputs["market_asof"], asof, stage)
    _same(inputs["market_evidence"], market_evidence, stage)
    market_by_symbol = {row["symbol"]: row for row in market_evidence}
    market_by_id = {row["id"]: row for row in market_evidence}
    outcomes, documents = semantic.company_evidence_from_frozen(cleaned, approved, replay.cutoff,
        security_ids=frontier["security_ids"], bundle=bundle)
    company = {"contract": semantic.COMPANY_CONTRACT, "bundle_id": bundle["bundle_id"],
               "manifest_sha256": bundle["manifest_sha256"], "candidates": outcomes}
    schema = decision_schema(symbols, list(documents), list(market_by_id),
        company_sources={symbol: row["source_ids"] for symbol, row in outcomes.items()})
    payload = semantic.plan_analysis_documents(cleaned, {
        "approved_picks": inputs["picks"], "watch_candidates": watch,
        "numeric_action": inputs["action"], "mrs": bound["mrs"], "sectors": bound["sectors"],
        "rejections": inputs["rejections"], "risk_rationale": inputs["rationale"],
        "market_evidence": market_evidence, "market_asof": asof,
        "company_evidence": {**company, "documents": list(documents.values())}}, schema, replay.planner(stage))
    calls = list(replay.io(stage))
    if len(calls) != 1:
        _fail(stage)
    output = replay.chat(stage, *calls[0], payload, schema)
    rows = output["decisions"]
    if len(rows) != len(symbols) or {row["symbol"] for row in rows} != set(symbols):
        _fail(stage)
    buys = {row["symbol"] for row in rows if row["action"] == "buy"}
    if (not buys <= picks.keys() or (output["action"] == "BUY") != bool(buys)
            or inputs["action"] == "AVOID" and (buys or output["action"] != "AVOID")):
        _fail(stage)
    from ..market_close import next_scheduled_close
    valid_for = next_scheduled_close(date.fromisoformat(replay.assessment["run_date"])).isoformat()
    instructions = []
    for row in rows:
        own_market = market_by_symbol[row["symbol"]]
        refs = row["source_ids"]
        news = [ref for ref in refs if ref in outcomes.get(row["symbol"], {}).get("source_ids", [])]
        if ([ref for ref in refs if ref in market_by_id] != [own_market["id"]]
                or len(news) + 1 != len(refs) or row["action"] == "buy" and not news):
            _fail(stage)
        pick = picks.get(row["symbol"]) if row["action"] == "buy" else None
        instructions.append({key: value for key, value in row.items() if key != "source_ids"} | {
            "target_weight": pick["position_pct"] if pick else 0,
            "sources": [{key: documents[ref][key] for key in ("id", "title", "url", "published_at")} for ref in news],
            "market_evidence": [own_market],
            **{key: pick[key] if pick else None for key in ("entry_price", "stop_price", "shares", "risk_usd")},
            "time_stop_days": pick["time_stop_days"] if pick and pick["time_stop_days"] > 0 else None,
            "valid_for": valid_for, "reference_capital_usd": inputs["account_usd"]})
    value = {"status": "model_required", "triggered": symbols, "evidence": {}, "model_output": output,
        "execution_decisions": instructions, "action_summary": output["action_summary"], "company_evidence": company}
    _same(replay.value(stage), value, stage)
    raw = result["raw"]
    _same(raw["debate"], value, stage)
    _same(raw["execution_decisions"], instructions, stage)
    _same(result["picks"], [pick for symbol, pick in picks.items() if symbol in buys], stage)
    if (result["action"] != output["action"] or result["market_view"] != output["action_summary"]
            or raw["action_summary"] != output["action_summary"]):
        _fail(stage)


def _partial_events(run, contract):
    """Verify an explicit completed prefix ending in one failed stage."""
    from .json_observation import CONTRACT

    raw = read_bytes(run, "events.jsonl", limit=16 * 1024 * 1024)
    if not raw.endswith(b"\n"):
        _fail()
    events = [strict_json(line) for line in raw.splitlines()]
    if not events or len(events) % 2 or len(events) > 2 * len(contract):
        _fail()
    previous, previous_output = None, None
    fields = {"schema", "run_id", "sequence", "stage_index", "stage", "kind", "status",
              "input_ref", "output_ref", "previous_stage_ref", "reason", "error_type",
              "previous_event_sha256", "event_sha256"}
    for index in range(len(events) // 2):
        begin, end = events[index * 2:index * 2 + 2]
        stage = contract[index]["name"]
        terminal = index == len(events) // 2 - 1
        for offset, row in enumerate((begin, end)):
            if (type(row) is not dict or set(row) != fields
                    or row["schema"] != "tiger.execution-event.v1" or row["run_id"] != run.name
                    or type(row["sequence"]) is not int or row["sequence"] != 2 * index + offset + 1
                    or type(row["stage_index"]) is not int or row["stage_index"] != index + 1
                    or row["stage"] != stage or row["kind"] != ("begin" if offset == 0 else "end")
                    or row["previous_event_sha256"] != previous
                    or row["previous_stage_ref"] != previous_output
                    or row["event_sha256"] != digest(json_bytes({key: value for key, value in row.items()
                                                               if key != "event_sha256"}))):
                _fail(stage)
            previous = row["event_sha256"]
        invalid_end = ((end["status"] != "failed" or type(end["error_type"]) is not str or not end["error_type"])
                       if terminal else (end["status"] != "completed" or end["error_type"] is not None))
        if (begin["status"] != "running" or begin["output_ref"] is not None
                or begin["reason"] is not None or begin["error_type"] is not None
                or begin["input_ref"] != end["input_ref"] or end["reason"] is not None
                or invalid_end):
            _fail(stage)
        read_snapshot(run, begin["input_ref"], run_id=run.name, stage=stage, role="input")
        output = read_snapshot(run, end["output_ref"], run_id=run.name, stage=stage, role="output")
        if terminal:
            expected = {"schema", "response_observation_contract", "io_calls"}
            valid = (type(output) is dict and set(output) == expected
                and output["schema"] == "tiger.failed-stage-output/v2"
                and output["response_observation_contract"] == CONTRACT)
            valid = valid or (type(output) is dict and set(output) == {"schema", "value", "io_calls"}
                and output["schema"] == "tiger.stage-output.v1")
        else:
            valid = (type(output) is dict and set(output) == {"schema", "value", "io_calls"}
                and output["schema"] == "tiger.stage-output.v1")
        if not valid or type(output["io_calls"]) is not list:
            _fail(stage)
        for number, call in enumerate(output["io_calls"], 1):
            if (type(call) is not dict or type(call.get("index")) is not int or call["index"] != number
                    or type(call.get("method")) is not str or not call["method"]
                    or any(char in call["method"] for char in "\r\n")
                    or ("output_ref" in call) == ("error_ref" in call)
                    or set(call) != {"index", "method", "input_ref",
                                     "error_ref" if "error_ref" in call else "output_ref"}):
                _fail(stage)
            read_snapshot(run, call["input_ref"], run_id=run.name, stage=stage, role="input")
            value = read_snapshot(run, call.get("output_ref", call.get("error_ref")),
                run_id=run.name, stage=stage, role="output")
            if "error_ref" in call:
                base = {"error_type", "message", "outcome_observed"}
                if (type(value) is not dict or not base.issubset(value)
                        or set(value) - base - {"diagnostic", "response_observation"}
                        or type(value["error_type"]) is not str or not value["error_type"]
                        or value["message"] != "External call failed"
                        or type(value["outcome_observed"]) is not bool):
                    _fail(stage)
                if "diagnostic" in value:
                    if (call["method"] not in {"model.deepseek.chat", "model.deepseek.search", "model.deepseek.fetch"}
                            or value["error_type"] != "RequiredModelUnavailable"):
                        _fail(stage)
                    validate_model_diagnostic(value["diagnostic"])
                if "response_observation" in value and (call["method"] != "model.deepseek.chat"
                        or value["error_type"] != "RequiredModelUnavailable"):
                    _fail(stage)
        previous_output = end["output_ref"]
    return events


def _partial_chat_request(request, stage, scene_policy_path):
    """Check the journal's entire wire and legal window, without business acceptance."""
    from .json_observation import CONTRACT

    thinking = scene_config(KERNEL_SCENE_MAP[stage], scene_policy_path)["thinking"]
    body = request["request"]
    rebuilt, estimate, ceiling = plan_request({key: value for key, value in body.items() if key != "max_tokens"})
    metadata = {"thinking_mode": thinking["type"], "reasoning_effort": thinking.get("effort"),
        "input_token_estimate": estimate, "output_ceiling": ceiling,
        "policy_version": POLICY_VERSION, "registry_version": REGISTRY_VERSION,
        "tokenizer_sha256": TOKENIZER_SHA256}
    if (request["schema"] != "tiger.deepseek-request.v2"
            or request["response_observation_contract"] != CONTRACT
            or type(request["call_id"]) is not str or re.fullmatch(r"[0-9a-f]{32}", request["call_id"]) is None
            or request["stage"] != stage or request["provider"] != "deepseek"
            or request["method"] != "POST" or request["endpoint"] != "https://api.deepseek.com/chat/completions"
            or request["release"] != RELEASE or request["scene"] != KERNEL_SCENE_MAP[stage]
            or request["request_sha256"] != _hash(body) or body != rebuilt
            or body.get("model") != MODEL or body.get("stream") is not False
            or body.get("response_format") != {"type": "json_object"}
            or body.get("thinking") != {"type": thinking["type"]}
            or (body.get("reasoning_effort") != thinking.get("effort")
                if thinking["type"] == "enabled" else "reasoning_effort" in body)
            or request["schema_sha256"] != _hash(request["output_schema"])
            or any(request.get(key) != value for key, value in metadata.items())):
        _fail(stage)
    return metadata


def verify_partial_intelligence_model(run, admission, *, scene_policy_path=None):
    """Account for explicitly bound failed-run observations; never certify a study."""
    try:
        from .json_observation import CONTRACT

        replay = _Replay(run, admission, scene_policy_path, _partial=True)
        if replay.observation_contract != CONTRACT:
            _fail()
        rows, stopped, other_model_io = [], False, 0
        for stage in replay.outputs:
            for call, request, proof in replay.io(stage):
                if stopped:
                    _fail(stage)
                if call["method"] != "model.deepseek.chat":
                    other_model_io += call["method"] in {"model.deepseek.search", "model.deepseek.fetch"}
                    continue
                if stage not in REQUIRED_MODEL_STAGES or request["call_id"] in replay.call_ids:
                    _fail(stage)
                replay.call_ids.add(request["call_id"])
                metadata = _partial_chat_request(request, stage, scene_policy_path)
                if "error_ref" in call:
                    observation = proof.get("response_observation")
                    if observation is None:
                        _fail(stage)
                    attempts = replay._observed_attempts(stage, request, observation)
                    if attempts[-1][2]["diagnostic"] is None:
                        _fail(stage)
                    _same(proof.get("diagnostic"), attempts[-1][2]["diagnostic"], stage)
                    stopped = True
                else:
                    if (proof["schema"] != "tiger.deepseek-call.v2" or proof["provider"] != "deepseek"
                            or proof["stage"] != stage or proof["call_id"] != request["call_id"]
                            or proof["model"] != MODEL or proof["requested_model"] != MODEL
                            or proof["release"] != RELEASE or proof["finish_reason"] != "stop"
                            or proof["scene"] != KERNEL_SCENE_MAP[stage]
                            or proof["request_sha256"] != request["request_sha256"]
                            or proof["schema_sha256"] != request["schema_sha256"]
                            or proof["search_mode"] != NATIVE_SEARCH_MODE
                            or any(proof.get(key) != value for key, value in metadata.items())):
                        _fail(stage)
                    attempts = replay._chat_observation(stage, request, proof)
                    replay.counts[stage] += 1
                for reference, native, result in attempts:
                    usage = result["usage"]
                    rows.append({"stage": stage, "call_id": request["call_id"],
                        "request_sha256": request["request_sha256"], "ordinal": reference["ordinal"],
                        "observation_ref": reference["observation_ref"],
                        "response_id": native["response"]["id"] if native["response"] is not None else None,
                        "diagnostic": result["diagnostic"], "state": "known_usage" if usage is not None else "usage_unknown",
                        "usage": usage, "requires_reconciliation": usage is None})
        return {"schema": "tiger.failed-model-accounting/v2", "run_id": replay.run.name,
            "response_observation_contract": CONTRACT, "business_completed": False,
            **replay.chat_accounting(other_model_io_count=other_model_io),
            "all_required_completed": False, "failure_stage": replay.events[-1]["stage"],
            "event_tail_sha256": replay.events[-1]["event_sha256"],
            "stage_counts": replay.counts, "observed_attempts": rows,
            "request_binding": "explicit-stage-IO-and-complete-wire-window",
            "scope": "Failed-run model observations and accounting only; business evidence is not certified."}
    except EvidenceInvalid:
        raise
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OSError, IntelligenceError,
            LLMUnavailable, RequiredModelFailed, TokenBudgetError, ExecutionContractError, ScenePolicyError, OverflowError):
        _fail()


def verify_intelligence_model(run, result, admission, *, scene_policy_path=None,
        source_content_contract=None, company_evidence_contract=None,
        event_contract=None, risk_evidence_contract=None, response_observation_contract=None):
    """Replay the new profile without changing the historical required proof."""
    try:
        for actual, expected in ((source_content_contract, SOURCE_CONTENT_CONTRACT),
                (company_evidence_contract, semantic.COMPANY_CONTRACT),
                (event_contract, STRUCTURED_EVENT_CONTRACT), (risk_evidence_contract, RISK_CONTRACT)):
            if actual is not None and actual != expected:
                _fail()
        replay = _Replay(run, admission, scene_policy_path)
        if response_observation_contract is not None and response_observation_contract != replay.observation_contract:
            _fail()
        if (result["raw"].get("paper_execution_version") == "tiger.paper-execution.v1") != replay.authoritative_paper:
            _fail()
        registry, archive, bundle, frontier, plan, snapshots, phases, market = _verify_acquisition(replay)
        cleaned, scored, stats = _verify_clean(replay, registry, archive, bundle, frontier, plan, snapshots, phases)
        signals = _verify_analysis(replay, scored)
        _verify_decision(replay, cleaned, frontier, bundle, market, result)
        if not all(replay.counts.values()):
            _fail()
        summary = {"provider": "deepseek", "model": MODEL, "release": RELEASE, "required": True,
            "response_ids": replay.response_ids, "call_count": len(replay.response_ids),
            "required_stages": list(REQUIRED_MODEL_STAGES), "stage_counts": replay.counts,
            "search_count": len(replay.native_searches), "fetch_count": len(replay.native_fetches),
            "search_mode": SEARCH_MODE, "all_required_completed": True,
            "acquisition_contract": ACQUISITION_CONTRACT, "bundle_id": bundle["bundle_id"],
            "source_requests": bundle["usage"]["requests"]}
        _same(result["raw"]["model"], summary)
        _same(result["raw"]["cross_validation"], stats)
        _same(result["raw"]["tech_signals"], [signal.to_dict() for signal in signals])
        reported = _plain(replay.value("report.emit"))
        for key in ("mrs", "sectors", "chains", "watchlist"):
            _same(result[key], reported[key], "report.emit")
        for key in ("scan_stats", "entry_budget", "security_roster", "security_identities"):
            if key in reported["raw"]:
                _same(result["raw"].get(key), reported["raw"][key], "report.emit")
        if (result["raw"]["docs_collected"] != len(project_to_rawdocuments(phases[1]))
                or result["raw"]["docs_cleaned"] != len(cleaned) or result["raw"]["docs_semantic_degraded"] != 0):
            _fail()
        raw = result["raw"]["intelligence"]
        _same(raw["exclusions"], reported["raw"]["intelligence"]["exclusions"], "report.emit")
        _same(raw["bundle"], {key: bundle[key] for key in ("bundle_id", "manifest_sha256", "state",
            "missing_required", "usage", "frozen_at", "frontier_manifest_hash")})
        _same(raw["frontier"], {key: frontier[key] for key in
            ("contract", "manifest_sha256", "symbols", "selection_reasons", "missing", "sector_gaps")})
        cells = {cell["cell_id"]: cell for cell in plan["coverage_cells"]}
        labels = {"global:macro": "全球宏观", "account:holdings": "已接入持仓"}
        labels.update({"chain:" + key: value["name"] for key, value in TECH_SUBCHAINS.items()})
        labels.update({"sector:" + target: target + " 板块" for target in semantic.TARGET_IDS if target not in TECH_SUBCHAINS})
        labels.update({"security:" + identity: symbol for symbol, identity in plan["universe"].items()})
        labels.update({"account:holding:" + symbol: symbol + " 持仓（不在审定池）"
                       for symbol in plan["unavailable_holdings"]})
        labels.update({"account:pending:" + symbol: symbol + " 待成交（不在审定池）"
                       for symbol in plan["unavailable_pending"]})
        disclosed_gaps = [{**row, "question": cells[row["cell_id"]]["question"],
            "material_type": cells[row["cell_id"]]["material_type"],
            "target_label": labels.get(row["target_id"], row["target_id"])} for row in bundle["gaps"]]
        if (raw["contract"] != ACQUISITION_CONTRACT or raw["job_id"] != bundle["job_id"]
                or raw["state"] != bundle["state"] or raw["usage"] != bundle["usage"]
                or raw["gaps"] != disclosed_gaps or raw["source_registry_sha256"] != registry.public_manifest()["sha256"]
                or raw["holdings_connection"] != ("unknown" if frontier["holdings"] is None else "connected")):
            _fail()
        packet = validate_input_packet(read_json(replay.run, "intelligence-input.json"))
        if acquisition_mode(packet["config"]) == "snapshot_only":
            if raw.get("acquisition_mode") != "snapshot_only":
                _fail()
        elif "acquisition_mode" in raw:
            _fail()
        if packet["baseline"] and packet["baseline"]["manifest"]["schema"] == "topic_snapshot/v1":
            manifest = packet["baseline"]["manifest"]
            _same(raw.get("baseline_snapshot"), {key: manifest[key] for key in
                ("snapshot_id", "manifest_sha256", "cutoff", "state", "counts", "gaps")})
        # Cheap binding failures reject before the more expensive numerical
        # replay. Every accepted proof still recomputes all eight rule stages.
        from ..intelligence_scoring_proof import verify_intelligence_scoring
        verify_intelligence_scoring(run, replay.ends, admission)
        if replay.observation_contract:
            summary = {**summary, "chat_accounting": replay.chat_accounting(
                other_model_io_count=len(replay.native_searches) + len(replay.native_fetches))}
        return summary
    except EvidenceInvalid:
        raise
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OSError, IntelligenceError,
            LLMUnavailable, RequiredModelFailed, TokenBudgetError, ExecutionContractError, ScenePolicyError, OverflowError):
        _fail()
