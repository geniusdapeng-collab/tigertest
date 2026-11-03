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
from .deepseek_required import (
    MODEL, RELEASE, SEARCH_MODE, retryable_search_failure, sanitize_url, validate_schema,
    DeepSeekRequiredClient,
)
from .model_budget import (
    POLICY_VERSION,
    REGISTRY_VERSION,
    TOKENIZER_SHA256,
    plan_request,
)
from .required_schemas import clean_schema, sentiment_schema, risk_schema, narrative_schema, decision_schema
from .required import SYSTEM, TASKS, _docs, market_asof
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


def verify_required_model(run, result: dict, admission: dict, *, scene_policy_path=None) -> dict:
    """No saved success flag is authoritative: walk bound stage IO again."""
    try:
        return _verify(run, result, admission, scene_policy_path=scene_policy_path)
    except EvidenceInvalid:
        raise
    except (KeyError, ValueError, TypeError, IndexError, LLMUnavailable,
            RequiredModelFailed, OverflowError):
        _fail()


def _verify(run, result, admission, *, scene_policy_path=None):
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
    chat_schema_retries = 0
    native_docs, decision_output, decision_inputs, decision_value = {}, None, None, None
    semantic_values, clean_expected, score_expected, collected_expected = {}, [], [], []
    def snapshot(row, role):
        return read_snapshot(run, row[role + "_ref"], run_id=run.name, stage=row["stage"], role=role)
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
                    or source["title"] != _normalize_text(native["title"])[:200]
                    or source["content"] != _normalize_text(native["content"])[:MAX_CONTENT]
                    or source["published"] != (native["published"] or "")):
                _fail()
        chat_index = 0
        for call in output["io_calls"]:
            if call["method"] not in {"model.deepseek.search", "model.deepseek.chat"} or "error_ref" in call:
                _fail()
            request = read_snapshot(run, call["input_ref"], run_id=run.name, stage=stage, role="input")
            proof = read_snapshot(run, call["output_ref"], run_id=run.name, stage=stage, role="output")
            call_id = request["call_id"]
            if (call_id in call_ids or not re.fullmatch(r"[0-9a-f]{32}", call_id)
                    or proof["call_id"] != call_id or request["stage"] != stage or proof["stage"] != stage
                    or request["provider"] != "deepseek" or proof["provider"] != "deepseek"
                    or request["method"] != "POST" or request["request_sha256"] != _hash(request["request"])
                    or proof["request_sha256"] != request["request_sha256"]
                    or not re.fullmatch(r"[0-9a-f]{64}", proof["transport_response_sha256"])):
                _fail()
            call_ids.add(call_id)
            if stage == "search.collect":
                expected_queries = [query + " as of " + admission["clock"]["run_date"]
                                    for query in {**CHAIN_TOPICS, **SECTOR_TOPICS}.values()]
                query = request["request"]["query"]
                retries = proof.get("retry_diagnostics", [])
                if (call["method"] != "model.deepseek.search" or query not in expected_queries
                        or request["schema"] != "tiger.deepseek-search-request.v1"
                        or proof["schema"] != "tiger.deepseek-search.v1"
                        or request["endpoint"] != "https://api.deepseek.com/v1/tools/search"
                        or request["request"] != {"query": query, "count": 4, "need_content": True}
                        or request["query_sha256"] != hashlib.sha256(query.encode()).hexdigest()
                        or proof["query_sha256"] != request["query_sha256"]
                        or proof["search_mode"] != SEARCH_MODE
                        or proof["response_sha256"] != _hash({"results": proof["results"]})
                        or type(retries) is not list or len(retries) > 1
                        or any(not retryable_search_failure(item) for item in retries)
                        or type(proof["result_count"]) is not int
                        or type(proof["usable_result_count"]) is not int
                        or proof["usable_result_count"] != len(proof["results"])
                        or not 0 <= proof["usable_result_count"] <= proof["result_count"] <= 4
                        or bool(proof["result_count"]) != bool(proof["usable_result_count"])):
                    _fail()
                searches.append(query)
                for doc in proof["results"]:
                    identity = {key: doc[key] for key in ("url", "title", "content", "published")}
                    if (sanitize_url(doc["url"]) != doc["url"] or len(doc["url"]) > 2048
                            or len(doc["title"]) > 500 or not doc["content"].strip()
                            or doc["id"] != "ds_" + _hash(identity)[:24]
                            or doc["content_hash"] != hashlib.sha256(doc["content"].encode()).hexdigest()
                            or doc["source"] != "deepseek_search"):
                        _fail()
                    if doc["id"] not in native_docs:
                        native_docs[doc["id"]] = doc
                        topic = next(name for name, text in {**CHAIN_TOPICS, **SECTOR_TOPICS}.items()
                                     if text + " as of " + admission["clock"]["run_date"] == query)
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
                selected_ids = ids[chat_index * 12:(chat_index + 1) * 12]
                expected_schema = clean_schema(selected_ids)
                expected_payload = {"documents": _docs(docs[chat_index * 12:(chat_index + 1) * 12])}
            elif stage == "tech.sentiment":
                selected_ids, expected_schema = ids, sentiment_schema(list(TECH_SUBCHAINS), ids)
                expected_payload = {"chains": list(TECH_SUBCHAINS), "documents": _docs(docs)}
            elif stage == "tech.risk":
                selected_ids, expected_schema = ids, risk_schema(list(TECH_SUBCHAINS), ids)
                expected_payload = {"chains": list(TECH_SUBCHAINS), "documents": _docs(docs)}
            elif stage == "sector.narrative":
                selected_ids, expected_schema = ids, narrative_schema(inputs["sector_symbols"], ids)
                expected_payload = {"etfs": inputs["sector_symbols"], "documents": _docs(docs)}
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
                expected_schema = decision_schema(approved + watch, ids, list(market_by_id))
                if (inputs.get("market_asof") != expected_market_asof
                        or inputs.get("market_evidence") != expected_market_evidence):
                    _fail()
                expected_payload = {
                    "approved_picks": inputs["picks"],
                    "watch_candidates": [row for row in inputs["watchlist"] if row["ticker"] not in approved][:5],
                    "numeric_action": inputs["action"], "mrs": bound_inputs["mrs"],
                    "sectors": bound_inputs["sectors"], "rejections": inputs["rejections"],
                    "risk_rationale": inputs["rationale"], "documents": _docs(docs),
                    "market_asof": expected_market_asof,
                    "market_evidence": expected_market_evidence}
            expected_payload["assessment"] = assessment
            body = request["request"]
            expected_messages = [
                {"role": "system", "content": SYSTEM + "\nReturn exactly one JSON object. JSON only."},
                {"role": "user", "content": TASKS[stage] + "\n" + json.dumps(normalize(expected_payload),
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    + "\nJSON schema:\n" + json.dumps(expected_schema, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"), allow_nan=False)
                    + "\nJSON field guidance:\n{}"},
            ]
            thinking = scene_config(KERNEL_SCENE_MAP[stage], scene_policy_path)["thinking"]
            thinking_mode = thinking["type"]
            reasoning_effort = thinking.get("effort")
            request_core = {"model": MODEL, "messages": expected_messages,
                "stream": False, "response_format": {"type": "json_object"},
                "thinking": {"type": thinking_mode}}
            if thinking_mode == "enabled":
                request_core["reasoning_effort"] = reasoning_effort
            else:
                request_core["temperature"] = 0.2
            expected_body, input_token_estimate, output_ceiling = plan_request(request_core)
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
            usage = proof["usage"]
            if DeepSeekRequiredClient._usage(usage, thinking_mode) != usage:
                _fail()
            if (any(type(usage[key]) is not int or usage[key] <= 0 for key in
                    ("prompt_tokens", "completion_tokens", "total_tokens"))
                    or usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]
                    or body["max_tokens"] <= 0 or body["max_tokens"] > output_ceiling):
                _fail()
            validate_schema(proof["parsed"], expected_schema)
            if stage == "clean.llm_semantic":
                rows, key, expected_ids = proof["parsed"]["documents"], "id", selected_ids
                by_id = {row["id"]: row for row in rows}
                for raw in docs[chat_index * 12:(chat_index + 1) * 12]:
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
        if stage != "search.collect" and counts[stage] != ((len(ids) + 11) // 12 if stage == "clean.llm_semantic" else 1):
            _fail()
        if stage == "search.collect":
            expected_collection = normalize({"documents": collected_expected,
                "topics": {**CHAIN_TOPICS, **SECTOR_TOPICS}, "batches": [],
                "cleanup": {"native_calls_completed": len(searches)}})
            if output["value"] != expected_collection:
                _fail()
            base_end = by_stage["clean.rule_base"]
            base_expected = rule_base_clean(collected_expected)
            if (snapshot(base_end, "input") != normalize({"documents": collected_expected})
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
    valid_for = datetime.combine(next_trading_day(admission["clock"]["run_date"]),
                                 time(16), ZoneInfo("America/New_York")).isoformat()
    expected = []
    decision_docs = {doc["raw"]["doc_id"]: doc["raw"] for doc in decision_inputs["documents"]}
    for row in decision_output["decisions"]:
        pick = picks[row["symbol"]] if row["action"] == "buy" else None
        own_market = market_by_symbol[row["symbol"]]
        market_refs = [source_id for source_id in row["source_ids"] if source_id in market_by_id]
        news_refs = [source_id for source_id in row["source_ids"] if source_id in decision_docs]
        if (market_refs != [own_market["id"]]
                or len(market_refs) + len(news_refs) != len(row["source_ids"])
                or len(news_refs) > 8 or row["action"] == "buy" and not news_refs):
            _fail()
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
    if decision_value != {"status": "model_required",
            "triggered": list(picks) + [row["ticker"] for row in decision_inputs["watchlist"] if row["ticker"] not in picks][:5],
            "evidence": {}, "model_output": decision_output,
            "execution_decisions": expected, "action_summary": decision_output["action_summary"]}:
        _fail()
    return summary
