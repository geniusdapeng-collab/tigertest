"""Read-only reconstruction of required-model evidence before publication."""
from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from ..agent_evidence import EvidenceInvalid, read_bytes, read_snapshot, strict_json, normalize
from ..calendar import next_trading_day
from ..cleaning.pipeline import _normalize_text, MAX_CONTENT, rule_base_clean
from ..search.credibility import CrossValidator, scoring_docs, evidence_for
from ..search.models import CleanDocument, RawDocument
from ..tech_chain.agents import SentimentRow, RiskAlert
from ..redline import LLMUnavailable
from ..redline.execution import REQUIRED_MODEL_STAGES, RequiredModelFailed
from ..redline.model_diagnostics import validate_model_diagnostic
from ..tech_chain.universe import CHAIN_TOPICS, SECTOR_TOPICS, TECH_SUBCHAINS
from .company_evidence import (
    COMPANY_EVIDENCE_CONTRACT, SEARCH_COUNT, candidate_evidence, candidate_searches,
)
from .deepseek_required import (
    MODEL, RELEASE, SEARCH_MODE, retryable_search_failure, sanitize_model_text,
    sanitize_model_value, sanitize_url, validate_schema,
    DeepSeekRequiredClient,
)
from .model_budget import (
    CONTEXT_TOKENS,
    POLICY_VERSION,
    REGISTRY_VERSION,
    TOKENIZER_SHA256,
    TokenBudgetError,
    chat_request_core,
    plan_request,
)
from .required_schemas import clean_schema, sentiment_schema, risk_schema, narrative_schema, decision_schema
from .required import (SYSTEM, TASKS, SOURCE_CONTENT_CONTRACT, _docs, chat_arguments,
                       market_asof, plan_analysis_documents, plan_clean_documents)
from .market_evidence import market_snapshots
from .scene_policy import KERNEL_SCENE_MAP, scene_config


def _fail():
    raise EvidenceInvalid("MODEL_REQUIRED_FAILED", "Required model/search evidence is missing or differs")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _plain(value):
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        if value.get("__type__") == "dataclass":
            return _plain(value["fields"])
        return {key: _plain(item) for key, item in value.items()}
    return value


def _same_watch_facts(actual, observed) -> bool:
    """RiskManager changes only these two candidate values before decision.

    Every candidate, field name and other value remains bound to layer3.tss;
    the exception does not permit omitting or inventing even a mutable field.
    """
    if type(actual) is not list or type(observed) is not list or len(actual) != len(observed):
        return False
    for current, original in zip(actual, observed):
        if (type(current) is not dict or type(original) is not dict
                or set(current) != set(original)
                or type(current.get("fields")) is not dict
                or type(original.get("fields")) is not dict
                or set(current["fields"]) != set(original["fields"])):
            return False
        if (_hash({key: value for key, value in current.items() if key != "fields"})
                != _hash({key: value for key, value in original.items() if key != "fields"})
                or _hash({key: value for key, value in current["fields"].items()
                          if key not in {"sector_etf", "tos"}})
                != _hash({key: value for key, value in original["fields"].items()
                          if key not in {"sector_etf", "tos"}})):
            return False
    return True


def _verify_search_request(request, query, stage, count):
    if (request["schema"] != "tiger.deepseek-search-request.v1"
            or request["stage"] != stage or request["provider"] != "deepseek"
            or request["endpoint"] != "https://api.deepseek.com/v1/tools/search"
            or request["method"] != "POST" or request["search_mode"] != SEARCH_MODE
            or request["request"] != {"query": query, "count": count, "need_content": True}
            or request["request_sha256"] != _hash(request["request"])
            or request["query_sha256"] != hashlib.sha256(query.encode()).hexdigest()):
        _fail()


def _verify_search_rows(proof, request, count):
    retries = proof.get("retry_diagnostics", [])
    if (proof["schema"] != "tiger.deepseek-search.v1"
            or proof["query_sha256"] != request["query_sha256"]
            or proof["search_mode"] != SEARCH_MODE
            or proof["response_sha256"] != _hash({"results": proof["results"]})
            or type(retries) is not list or len(retries) > 1
            or any(not retryable_search_failure(item) for item in retries)
            or type(proof["results"]) is not list
            or type(proof["result_count"]) is not int
            or type(proof["usable_result_count"]) is not int
            or proof["usable_result_count"] != len(proof["results"])
            or not 0 <= proof["usable_result_count"] <= proof["result_count"] <= count
            or bool(proof["result_count"]) != bool(proof["usable_result_count"])):
        _fail()
    for doc in proof["results"]:
        identity = {key: doc[key] for key in ("url", "title", "content", "published")}
        if (sanitize_url(doc["url"]) != doc["url"] or len(doc["url"]) > 2048
                or not 1 <= len(doc["title"]) <= 500 or not doc["content"].strip()
                or doc["id"] != "ds_" + _hash(identity)[:24]
                or doc["content_hash"] != hashlib.sha256(doc["content"].encode()).hexdigest()
                or doc["source"] != "deepseek_search"):
            _fail()
    return proof["results"]


def verify_required_model(run, result: dict, admission: dict, *, scene_policy_path=None,
                          source_content_contract=None, company_evidence_contract=None) -> dict:
    """No saved success flag is authoritative: walk bound stage IO again."""
    try:
        return _verify(run, result, admission, scene_policy_path=scene_policy_path,
                       source_content_contract=source_content_contract,
                       company_evidence_contract=company_evidence_contract)
    except EvidenceInvalid:
        raise
    except (KeyError, ValueError, TypeError, IndexError, LLMUnavailable,
            RequiredModelFailed, TokenBudgetError, OverflowError):
        _fail()


def _verify(run, result, admission, *, scene_policy_path=None, source_content_contract=None,
            company_evidence_contract=None):
    clock = admission["clock"]
    run_date, market_timezone = clock["run_date"], clock["market_timezone"]
    if (type(run_date) is not str or date.fromisoformat(run_date).isoformat() != run_date
            or type(market_timezone) is not str or market_timezone != "America/New_York"):
        _fail()
    assessment = {"run_date": run_date, "market_timezone": market_timezone}
    events = [strict_json(line) for line in read_bytes(run, "events.jsonl").splitlines()]
    by_stage = {row["stage"]: row for row in events if row["kind"] == "end"}
    counts = {stage: 0 for stage in REQUIRED_MODEL_STAGES}
    response_ids, call_ids, searches = [], set(), []
    response_identity_hashes = set()
    chat_schema_retries = 0
    native_docs, decision_output, decision_inputs, decision_value = {}, None, None, None
    semantic_values, clean_expected, score_expected, collected_expected = {}, [], [], []
    def snapshot(row, role):
        return read_snapshot(run, row[role + "_ref"], run_id=run.name, stage=row["stage"], role=role)
    base_input = snapshot(by_stage["clean.rule_base"], "input")
    observed_contract = base_input.get("source_content_contract")
    expected_contract = {None: observed_contract, "legacy-excerpts/v1": None,
                         SOURCE_CONTENT_CONTRACT: SOURCE_CONTENT_CONTRACT}
    if (observed_contract not in (None, SOURCE_CONTENT_CONTRACT)
            or source_content_contract not in expected_contract
            or observed_contract != expected_contract[source_content_contract]):
        _fail()
    legacy = observed_contract is None
    company_value = snapshot(by_stage["decision.debate"], "output")["value"].get("company_evidence")
    observed_company = company_value["contract"] if company_value is not None else ""
    if (observed_company not in ("", COMPANY_EVIDENCE_CONTRACT)
            or company_evidence_contract is not None and observed_company != company_evidence_contract
            or legacy and observed_company):
        _fail()
    company_enabled = bool(observed_company)
    company_outcomes, company_documents = {}, {}
    admitted_at = (datetime.fromisoformat(clock["admitted_at_utc"])
                   if company_enabled and "admitted_at_utc" in clock else None)
    if admitted_at is not None and admitted_at.utcoffset() is None:
        _fail()
    for stage in ("search.collect", *REQUIRED_MODEL_STAGES):
        end = by_stage[stage]
        if end["status"] != "completed" or end["error_type"] is not None or end["reason"] is not None:
            _fail()
        bound_inputs = snapshot(end, "input")
        inputs = _plain(bound_inputs)
        if stage != "search.collect" and inputs.get("assessment") != assessment:
            _fail()
        output = read_snapshot(run, end["output_ref"], run_id=run.name, stage=stage, role="output")
        docs = inputs.get("documents", [])
        ids = [doc.get("raw", doc)["doc_id"] for doc in docs]
        if stage not in {"search.collect", "clean.llm_semantic"}:
            expected_documents = score_expected if stage in {"tech.sentiment", "sector.narrative"} else clean_expected
            if bound_inputs["documents"] != normalize(expected_documents):
                _fail()
        if stage != "search.collect" and (not ids or len(set(ids)) != len(ids) or not set(ids).issubset(native_docs)):
            _fail()
        for doc in docs:
            source = doc.get("raw", doc)
            native = native_docs[source["doc_id"]]
            if (source["url"] != native["url"] or source["source"] != "deepseek_search"
                    or source["title"] != (_normalize_text(native["title"])[:200] if legacy else native["title"])
                    or source["content"] != (_normalize_text(native["content"])[:MAX_CONTENT] if legacy else native["content"])
                    or source["published"] != (native["published"] or "")):
                _fail()
        if stage != "search.collect":
            thinking = scene_config(KERNEL_SCENE_MAP[stage], scene_policy_path)["thinking"]
            thinking_mode = thinking["type"]
            reasoning_effort = thinking.get("effort")

            def plan(payload, schema):
                if legacy:
                    system = SYSTEM
                    user = TASKS[stage] + "\n" + json.dumps(normalize({"assessment": assessment, **payload}),
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                else:
                    args = chat_arguments(stage, payload, schema, assessment)
                    system = sanitize_model_text(args["system"])
                    user = sanitize_model_text(args["user"]) + "\n" + json.dumps(
                        sanitize_model_value(args["public_payload"]), ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"), allow_nan=False)
                return plan_request(chat_request_core(system=system, user=user, schema=schema,
                    schema_hint={}, thinking_mode=thinking_mode, effort=reasoning_effort, temperature=0.2))

        if stage == "clean.llm_semantic":
            if legacy:
                batches = [(batch, {"documents": _docs(batch, legacy=True)}, clean_schema(ids[start:start + 12]))
                           for start in range(0, len(docs), 12) if (batch := docs[start:start + 12])]
            else:
                batches = plan_clean_documents(docs, plan)
        company_queries = []
        if company_enabled and stage == "decision.debate":
            approved_symbols = [pick["fields"]["ticker"]
                                for pick in snapshot(by_stage["layer4.risk"], "output")["value"]["picks"]]
            company_queries = list(candidate_searches(approved_symbols, date.fromisoformat(run_date)).items())
        chat_index = 0
        for call in output["io_calls"]:
            method = call["method"]
            if method not in {"model.deepseek.search", "model.deepseek.chat"}:
                _fail()
            request = read_snapshot(run, call["input_ref"], run_id=run.name, stage=stage, role="input")
            call_id = request["call_id"]
            if call_id in call_ids or not re.fullmatch(r"[0-9a-f]{32}", call_id):
                _fail()
            call_ids.add(call_id)
            company_search = company_enabled and stage == "decision.debate" and method == "model.deepseek.search"
            if company_search:
                if chat_index or len(company_outcomes) >= len(company_queries):
                    _fail()
                symbol, query = company_queries[len(company_outcomes)]
                _verify_search_request(request, query, stage, SEARCH_COUNT)
            if "error_ref" in call:
                if not company_search:
                    _fail()
                error = read_snapshot(run, call["error_ref"], run_id=run.name, stage=stage, role="output")
                if (set(error) != {"error_type", "message", "outcome_observed", "diagnostic"}
                        or error["error_type"] != "RequiredModelUnavailable"
                        or error["message"] != "External call failed" or error["outcome_observed"] is not True):
                    _fail()
                outcome, _ = candidate_evidence(symbol, [], date.fromisoformat(run_date),
                                                admitted_at, failure=error["diagnostic"])
                company_outcomes[symbol] = outcome
                continue
            proof = read_snapshot(run, call["output_ref"], run_id=run.name, stage=stage, role="output")
            if (proof["call_id"] != call_id or request["stage"] != stage or proof["stage"] != stage
                    or request["provider"] != "deepseek" or proof["provider"] != "deepseek"
                    or request["method"] != "POST" or request["request_sha256"] != _hash(request["request"])
                    or proof["request_sha256"] != request["request_sha256"]
                    or not re.fullmatch(r"[0-9a-f]{64}", proof["transport_response_sha256"])):
                _fail()
            if company_search:
                rows = _verify_search_rows(proof, request, SEARCH_COUNT)
                outcome, filings = candidate_evidence(symbol, rows, date.fromisoformat(run_date), admitted_at)
                company_outcomes[symbol] = outcome
                company_documents.update((row["id"], row) for row in filings)
                continue
            if stage == "search.collect":
                expected_queries = [query + " as of " + run_date
                                    for query in {**CHAIN_TOPICS, **SECTOR_TOPICS}.values()]
                query = request["request"]["query"]
                if method != "model.deepseek.search" or query not in expected_queries:
                    _fail()
                _verify_search_request(request, query, stage, 4)
                searches.append(query)
                for doc in _verify_search_rows(proof, request, 4):
                    if doc["id"] not in native_docs:
                        native_docs[doc["id"]] = doc
                        topic = next(name for name, text in {**CHAIN_TOPICS, **SECTOR_TOPICS}.items()
                                     if text + " as of " + run_date == query)
                        collected_expected.append(RawDocument(doc["id"], "deepseek_search", doc["title"],
                            doc["url"], doc["content"], doc["published"] or "",
                            datetime.fromisoformat(doc["fetched_at"].replace("Z", "+00:00")).timestamp(),
                            {"topic": topic, "search_mode": SEARCH_MODE,
                             "native_content_sha256": doc["content_hash"]}))
                continue
            if call["method"] != "model.deepseek.chat":
                _fail()
            retries = proof.get("retry_diagnostics", [])
            if (type(retries) is not list or chat_schema_retries + len(retries) > 1
                    or any(validate_model_diagnostic(item)["code"] != "OUTPUT_SCHEMA_MISMATCH"
                           or "schema_keyword" not in item for item in retries)):
                _fail()
            chat_schema_retries += len(retries)
            if stage == "clean.llm_semantic":
                batch, expected_payload, expected_schema = batches[chat_index]
                selected_ids = [item["doc_id"] for item in batch]
            elif stage == "tech.sentiment":
                selected_ids, expected_schema = ids, sentiment_schema(list(TECH_SUBCHAINS), ids)
                expected_payload = {"chains": list(TECH_SUBCHAINS)}
            elif stage == "tech.risk":
                selected_ids, expected_schema = ids, risk_schema(list(TECH_SUBCHAINS), ids)
                expected_payload = {"chains": list(TECH_SUBCHAINS)}
            elif stage == "sector.narrative":
                selected_ids, expected_schema = ids, narrative_schema(inputs["sector_symbols"], ids)
                expected_payload = {"etfs": inputs["sector_symbols"]}
            else:
                selected_ids = ids
                risk_value = snapshot(by_stage["layer4.risk"], "output")["value"]
                observed_watch = snapshot(by_stage["layer3.tss"], "output")["value"]["watchlist"]
                frozen_facts = ((bound_inputs["picks"], risk_value["picks"]),
                    (bound_inputs["mrs"], snapshot(by_stage["layer1.mrs"], "output")["value"]),
                    (bound_inputs["sectors"], snapshot(by_stage["layer2.sector"], "output")["value"]["sectors"]),
                    *((bound_inputs[key], risk_value[source]) for key, source in (
                        ("action", "action"), ("rationale", "pick_rationale"), ("rejections", "rejections"))))
                if (not _same_watch_facts(bound_inputs["watchlist"], observed_watch)
                        or any(_hash(actual) != _hash(observed) for actual, observed in frozen_facts)):
                    _fail()
                approved = [pick["ticker"] for pick in inputs["picks"]]
                watch = [row["ticker"] for row in inputs["watchlist"] if row["ticker"] not in approved][:5]
                decision_inputs, decision_value = inputs, output["value"]
                prepared_market_data = snapshot(by_stage["data.prepare"], "output")["value"]["market_data"]
                expected_market_asof = market_asof(prepared_market_data, approved + watch, assessment)
                expected_market_evidence = market_snapshots(prepared_market_data, approved + watch, assessment)
                market_by_symbol = {row["symbol"]: row for row in expected_market_evidence}
                market_by_id = {row["id"]: row for row in expected_market_evidence}
                if company_enabled:
                    if (list(company_outcomes) != approved
                            or company_value != {"contract": COMPANY_EVIDENCE_CONTRACT,
                                                 "candidates": company_outcomes}):
                        _fail()
                    expected_schema = decision_schema(approved + watch, list(company_documents), list(market_by_id),
                        company_sources={symbol: outcome["source_ids"] for symbol, outcome in company_outcomes.items()})
                else:
                    expected_schema = decision_schema(approved + watch, ids, list(market_by_id))
                if (inputs.get("market_asof") != expected_market_asof
                        or inputs.get("market_evidence") != expected_market_evidence):
                    _fail()
                expected_payload = {
                    "approved_picks": inputs["picks"],
                    "watch_candidates": [row for row in inputs["watchlist"] if row["ticker"] not in approved][:5],
                    "numeric_action": inputs["action"], "mrs": bound_inputs["mrs"],
                    "sectors": bound_inputs["sectors"], "rejections": inputs["rejections"],
                    "risk_rationale": inputs["rationale"],
                    "market_asof": expected_market_asof,
                    "market_evidence": expected_market_evidence}
                if company_enabled:
                    expected_payload["company_evidence"] = {
                        **company_value, "documents": list(company_documents.values())}
            if stage != "clean.llm_semantic":
                if legacy:
                    expected_payload["documents"] = _docs(docs, legacy=True)
                else:
                    expected_payload = plan_analysis_documents(docs, expected_payload, expected_schema, plan)
            body = request["request"]
            expected_body, input_token_estimate, output_ceiling = plan(expected_payload, expected_schema)
            if (request["endpoint"] != "https://api.deepseek.com/chat/completions"
                    or request["schema"] != "tiger.deepseek-request.v1"
                    or proof["schema"] != "tiger.deepseek-call.v1"
                    or request["release"] != RELEASE or body["model"] != MODEL
                    or request.get("scene") != KERNEL_SCENE_MAP[stage]
                    or request.get("thinking_mode") != thinking_mode
                    or request.get("reasoning_effort") != reasoning_effort
                    or request.get("input_token_estimate") != input_token_estimate
                    or request.get("output_ceiling") != output_ceiling
                    or request.get("policy_version") != POLICY_VERSION
                    or request.get("registry_version") != REGISTRY_VERSION
                    or request.get("tokenizer_sha256") != TOKENIZER_SHA256
                    or body["thinking"] != {"type": thinking_mode}
                    or ("reasoning_effort" in body) != (thinking_mode == "enabled")
                    or body.get("reasoning_effort") not in (None, reasoning_effort)
                    or body["response_format"] != {"type": "json_object"} or body["stream"] is not False
                    or proof.get("thinking_mode") != thinking_mode
                    or proof.get("reasoning_effort") != reasoning_effort
                    or proof.get("input_token_estimate") != input_token_estimate
                    or proof.get("output_ceiling") != output_ceiling
                    or proof.get("policy_version") != POLICY_VERSION
                    or proof.get("registry_version") != REGISTRY_VERSION
                    or proof.get("tokenizer_sha256") != TOKENIZER_SHA256
                    or request["output_schema"] != expected_schema
                    or request["schema_sha256"] != _hash(expected_schema)
                    or proof["schema_sha256"] != request["schema_sha256"]
                    or proof["model"] != MODEL or proof["requested_model"] != MODEL
                    or proof["release"] != RELEASE or proof["finish_reason"] != "stop"
                    or proof["search_mode"] != SEARCH_MODE or body != expected_body):
                _fail()
            response = proof["response"]
            if (proof["response_sha256"] != _hash(response) or response["id"] != proof["response_id"]
                    or response["model"] != MODEL or response["usage"] != proof["usage"]
                    or response["system_fingerprint"] != proof["system_fingerprint"]
                    or response["choices"] != [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": json.dumps(proof["parsed"], ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False)}}]
                    or proof["response_id"] in response_ids
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", proof["response_id"])):
                _fail()
            identity_hash = hashlib.sha256(proof["response_id"].encode()).hexdigest()
            if identity_hash in response_identity_hashes:
                _fail()
            response_identity_hashes.add(identity_hash)
            attempt = proof.get("retry_attempt")
            if not legacy and bool(retries) != (attempt is not None):
                _fail()
            if attempt is not None:
                if (not retries or set(attempt) != {"response_id_sha256", "transport_response_sha256",
                            "request_sha256", "finish_reason", "model", "usage"}
                        or not re.fullmatch(r"[0-9a-f]{64}", attempt["response_id_sha256"])
                        or not re.fullmatch(r"[0-9a-f]{64}", attempt["transport_response_sha256"])
                        or attempt["response_id_sha256"] in response_identity_hashes
                        or attempt["transport_response_sha256"] == proof["transport_response_sha256"]
                        or attempt["request_sha256"] != request["request_sha256"]
                        or attempt["finish_reason"] != "stop" or attempt["model"] != MODEL
                        or DeepSeekRequiredClient._usage(attempt["usage"], thinking_mode) != attempt["usage"]
                        or attempt["usage"]["completion_tokens"] > output_ceiling
                        or attempt["usage"]["total_tokens"] > CONTEXT_TOKENS):
                    _fail()
                response_identity_hashes.add(attempt["response_id_sha256"])
            usage = proof["usage"]
            if DeepSeekRequiredClient._usage(usage, thinking_mode) != usage:
                _fail()
            if (any(type(usage[key]) is not int or usage[key] <= 0 for key in
                    ("prompt_tokens", "completion_tokens", "total_tokens"))
                    or usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]
                    or usage["completion_tokens"] > output_ceiling or usage["total_tokens"] > CONTEXT_TOKENS
                    or body["max_tokens"] <= 0 or body["max_tokens"] > output_ceiling):
                _fail()
            validate_schema(proof["parsed"], expected_schema)
            if stage == "clean.llm_semantic":
                rows, key, expected_ids = proof["parsed"]["documents"], "id", selected_ids
                by_id = {row["id"]: row for row in rows}
                for raw in batch:
                    ann = by_id[raw["doc_id"]]
                    original = RawDocument(**raw)
                    clean_expected.append(CleanDocument(raw=original,
                        **{name: value for name, value in ann.items() if name != "id"},
                        degraded=False, evidence=evidence_for(original)))
            elif stage == "tech.sentiment":
                rows, key, expected_ids = proof["parsed"]["chains"], "chain_id", list(TECH_SUBCHAINS)
                semantic_values[stage] = normalize({row["chain_id"]: SentimentRow(
                    **{name: value for name, value in row.items() if name != "evidence_ids"}, degraded=False) for row in rows})
            elif stage == "sector.narrative":
                rows, key, expected_ids = proof["parsed"]["sectors"], "etf", inputs["sector_symbols"]
                narrative = {row["etf"]: {"score": row["narrative_score"], "bias": row["eps_revision_bias"],
                    "tone": row["guidance_tone"], "evidence": "; ".join(row["evidence"]),
                    "evidence_ids": row["evidence_ids"]} for row in rows}
                semantic_values[stage] = {"raw_result": narrative, "narrative": narrative}
            elif stage == "decision.debate":
                rows, key, expected_ids = proof["parsed"]["decisions"], "symbol", approved + watch
                decision_output = proof["parsed"]
            else:
                rows, key, expected_ids = [], "unused", []
                semantic_values[stage] = normalize([RiskAlert(**row) for row in proof["parsed"]["alerts"]])
            if len(rows) != len(expected_ids) or {row[key] for row in rows} != set(expected_ids):
                _fail()
            response_ids.append(proof["response_id"])
            counts[stage] += 1
            chat_index += 1
        if stage != "search.collect" and counts[stage] != (len(batches) if stage == "clean.llm_semantic" else 1):
            _fail()
        if stage == "search.collect":
            expected_collection = normalize({"documents": collected_expected,
                "topics": {**CHAIN_TOPICS, **SECTOR_TOPICS}, "batches": [],
                "cleanup": {"native_calls_completed": len(searches)}})
            if output["value"] != expected_collection:
                _fail()
            base_end = by_stage["clean.rule_base"]
            base_expected = rule_base_clean(collected_expected, preserve_source=not legacy)
            expected_base_input = {"documents": collected_expected}
            if not legacy:
                expected_base_input["source_content_contract"] = SOURCE_CONTENT_CONTRACT
            if (base_input != normalize(expected_base_input)
                    or snapshot(base_end, "output")["value"] != normalize({"documents": base_expected})
                    or snapshot(by_stage["clean.llm_semantic"], "input") != normalize({
                        "documents": base_expected, "assessment": assessment})):
                _fail()
        elif stage == "clean.llm_semantic":
            if output["value"] != normalize({"documents": clean_expected, "degraded_count": 0}):
                _fail()
            cross_end = by_stage["clean.cross_validate"]
            if snapshot(cross_end, "input") != normalize({"documents": clean_expected}):
                _fail()
            cv_stats = CrossValidator().validate(clean_expected)
            score_expected = scoring_docs(clean_expected)
            expected_cross = normalize({"documents": clean_expected, "scoring_documents": score_expected, "stats": cv_stats})
            if snapshot(cross_end, "output")["value"] != expected_cross:
                _fail()
        elif stage in {"tech.sentiment", "tech.risk", "sector.narrative"}:
            if output["value"] != semantic_values[stage]:
                _fail()
    if len(searches) != len(set(searches)) or set(searches) != set(expected_queries):
        _fail()
    fusion = snapshot(by_stage["tech.fusion"], "input")
    sector = snapshot(by_stage["layer2.sector"], "input")
    if (fusion["sentiment"] != semantic_values["tech.sentiment"]
            or fusion["alerts"] != semantic_values["tech.risk"]
            or sector["narrative"] != semantic_values["sector.narrative"]["narrative"]):
        _fail()
    summary = {"provider": "deepseek", "model": MODEL, "release": RELEASE,
        "required": True, "response_ids": response_ids, "call_count": len(response_ids),
        "required_stages": list(REQUIRED_MODEL_STAGES), "stage_counts": counts,
        "search_count": len(searches), "search_mode": SEARCH_MODE, "all_required_completed": True}
    raw = result["raw"]
    if raw["model"] != summary or raw["action_summary"] != decision_output["action_summary"]:
        _fail()
    if (result["action"] != decision_output["action"] or result["market_view"] != raw["action_summary"]
            or decision_value["model_output"] != decision_output
            or decision_value["execution_decisions"] != raw["execution_decisions"]):
        _fail()
    picks = {pick["ticker"]: pick for pick in decision_inputs["picks"]}
    buys = {row["symbol"] for row in decision_output["decisions"] if row["action"] == "buy"}
    if (not buys.issubset(picks) or (result["action"] == "BUY") != bool(buys)
            or decision_inputs["action"] == "AVOID" and (buys or result["action"] != "AVOID")
            or result["picks"] != [pick for symbol, pick in picks.items() if symbol in buys]):
        _fail()
    if prepared_market_data["freshness"].get("market_close"):
        from ..market_close import next_scheduled_close
        from ..redline.execution import StrictObservationError

        try:
            valid_for = next_scheduled_close(date.fromisoformat(run_date)).isoformat()
        except StrictObservationError:
            _fail()
    else:
        valid_for = datetime.combine(next_trading_day(run_date), time(16), ZoneInfo("America/New_York")).isoformat()
    expected = []
    decision_docs = {doc["raw"]["doc_id"]: doc["raw"] for doc in decision_inputs["documents"]}
    for row in decision_output["decisions"]:
        pick = picks[row["symbol"]] if row["action"] == "buy" else None
        own_market = market_by_symbol[row["symbol"]]
        market_refs = [source_id for source_id in row["source_ids"] if source_id in market_by_id]
        allowed = (company_outcomes.get(row["symbol"], {}).get("source_ids", [])
                   if company_enabled else decision_docs)
        news_refs = [source_id for source_id in row["source_ids"] if source_id in allowed]
        if (market_refs != [own_market["id"]]
                or len(market_refs) + len(news_refs) != len(row["source_ids"])
                or len(news_refs) > 8 or row["action"] == "buy" and not news_refs):
            _fail()
        if company_enabled:
            sources = [{key: company_documents[source_id][key] for key in ("id", "title", "url", "published_at")}
                       for source_id in news_refs]
        else:
            sources = [{"id": source_id, "title": decision_docs[source_id]["title"],
                "url": decision_docs[source_id]["url"], "published_at": decision_docs[source_id]["published"] or None}
                for source_id in news_refs]
        expected.append({key: value for key, value in row.items() if key != "source_ids"} | {
            "target_weight": pick["position_pct"] if pick else 0, "sources": sources,
            "market_evidence": [own_market],
            **{key: pick[key] if pick else None for key in ("entry_price", "stop_price", "shares", "risk_usd")},
            "time_stop_days": pick["time_stop_days"] if pick and pick["time_stop_days"] > 0 else None,
            "valid_for": valid_for, "reference_capital_usd": 100000.0})
    if raw["execution_decisions"] != expected:
        _fail()
    expected_value = {"status": "model_required",
            "triggered": list(picks) + [row["ticker"] for row in decision_inputs["watchlist"] if row["ticker"] not in picks][:5],
            "evidence": {}, "model_output": decision_output,
            "execution_decisions": expected, "action_summary": decision_output["action_summary"]}
    if company_enabled:
        expected_value["company_evidence"] = company_value
        if raw["debate"].get("company_evidence") != company_value:
            _fail()
    if decision_value != expected_value:
        _fail()
    return summary
