"""Bounded collection, original evidence, explicit gaps and immutable freezes."""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import nullcontext
from datetime import timedelta
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from .adapters import DeepSeekBridge
from .archive import Archive
from .contracts import (DOCUMENT_SCHEMA, FetchResult, IntelligenceError, SearchRequest,
                        SearchResult, SourceError, aware, canonical, canonical_url,
                        digest, safe_value, source_time, timestamp, utc_now)
from .planner import compile_requests
from .registry import default_registry


def _parameters(value):
    names = set(SearchRequest.__dataclass_fields__)
    return SearchRequest(**{k: tuple(v) if k in {"domains", "entities", "targets"} else v
                            for k, v in value.items() if k in names})


def _blob_refs(value):
    if isinstance(value, dict):
        if {"path", "sha256", "bytes"} <= value.keys() and re.fullmatch(r"[0-9a-f]{64}", str(value["sha256"])):
            yield value
        for child in value.values():
            yield from _blob_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _blob_refs(child)


class IntelligenceService:
    def __init__(self, archive_root, registry=None, adapters=None, *, tenant="public",
                 clock=utc_now, observe_io=None, readonly=False, sleeper=time.sleep):
        self.registry = registry or default_registry()
        self.adapters = adapters if adapters is not None else ({} if readonly else self.registry.build_adapters())
        self.archive = Archive(archive_root, tenant=tenant, clock=clock, readonly=readonly)
        self.clock, self.sleeper = clock, sleeper
        self.observe_io = observe_io or (lambda method, inputs, invoke: invoke())
        self._next_source = {}
        self._deadlines = {}
        self.readonly = readonly

    def _write(self):
        if self.readonly:
            raise IntelligenceError("ACCESS_DENIED", "Acquisition service is read only")

    def create_job(self, plan, replay_key):
        self._write()
        if plan.get("source_registry_sha256") != self.registry.public_manifest()["sha256"]:
            raise IntelligenceError("PLAN_CONFLICT", "Plan uses another registered source policy")
        if safe_value(plan, self._secrets()) != plan:
            raise IntelligenceError("POLICY_DENIED", "Credentials cannot be persisted in an acquisition plan")
        return self.archive.create_job(plan, replay_key)

    def status(self, job_id):
        return self.archive.status(job_id)

    def _deadline(self, job_id):
        if job_id not in self._deadlines:
            status, plan = self.status(job_id), self.archive.plan(job_id)
            remaining = plan["limits"]["deadline_seconds"] - (aware(self.clock()) - aware(status["created_at"])).total_seconds()
            self._deadlines[job_id] = time.monotonic() + max(0.0, remaining)
        return self._deadlines[job_id]

    def _secrets(self):
        values = [getattr(adapter, "credential", None) for adapter in self.adapters.values()]
        values.extend(getattr(adapter.client, "_api_key", None) for adapter in self.adapters.values() if isinstance(adapter, DeepSeekBridge))
        return tuple(value for value in values if isinstance(value, str) and value)

    def _safe_raw(self, raw):
        original = raw
        for secret in self._secrets():
            raw = raw.replace(secret.encode(), b"[REDACTED_SECRET]")
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                return raw, raw != original
            cleaned = safe_value(text, self._secrets())
            if cleaned != text:
                raw = cleaned.encode()
        else:
            cleaned = safe_value(parsed, self._secrets())
            if cleaned != parsed:
                raw = canonical(cleaned)
        return raw, raw != original

    def _observe_result(self, source_id, operation, parameters, result):
        spec = self.registry.get(source_id)
        secrets = self._secrets()
        if isinstance(result, SearchResult):
            raw = result.raw_bytes or canonical(result.to_dict())
            raw, redacted = self._safe_raw(raw)
            ref = self.archive.put_blob(raw, "application/json" if spec.kind in {"search", "sec_submissions", "sec_directory", "fred"} else "application/octet-stream")
            normalized = safe_value(result.to_dict(), secrets)
            metadata = result.response_metadata
            received = metadata.get("received_at") or timestamp(self.clock())
            transport_sha = metadata.get("transport_sha256") or hashlib.sha256(raw).hexdigest()
            count = metadata.get("request_count", 1)
            if isinstance(self.adapters[source_id], DeepSeekBridge):
                receipt = metadata.get("receipt") or {}
                count = 1 + len(receipt.get("retry_diagnostics", []))
            return {"schema": "tiger.intelligence-source-response/v1", "source_id": source_id,
                "operation": operation, "request_sha256": digest(parameters), "raw_response_ref": ref,
                "transport_sha256": transport_sha, "raw_redacted": redacted,
                "response_url": metadata.get("response_url", spec.endpoint),
                "http_headers": metadata.get("http_headers", {}), "received_at": received,
                "actual_request": safe_value(result.request, secrets), "normalized": normalized,
                "usage": safe_value(result.usage, secrets), "request_count": count,
                "bytes_received": len(result.raw_bytes) if result.raw_bytes else len(raw),
                "normalization_kind": "deepseek_bridge" if isinstance(self.adapters[source_id], DeepSeekBridge) else "registered_adapter"}
        if not isinstance(result, FetchResult):
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        raw, redacted = self._safe_raw(result.raw_bytes)
        ref = self.archive.put_blob(raw, result.media_type)
        metadata = safe_value(result.metadata, secrets)
        normalized = safe_value({k: v for k, v in asdict(result).items() if k != "raw_bytes"}, secrets)
        return {"schema": "tiger.intelligence-source-response/v1", "source_id": source_id,
            "operation": operation, "request_sha256": digest(parameters), "raw_response_ref": ref,
            "transport_sha256": metadata.get("transport_sha256", hashlib.sha256(raw).hexdigest()),
            "raw_redacted": redacted, "response_url": result.final_url,
            "http_headers": metadata.get("http_headers", {"content-type": result.media_type}),
            "received_at": metadata.get("retrieved_at", timestamp(self.clock())),
            "actual_request": parameters, "normalized": normalized,
            "usage": safe_value(result.usage, secrets), "request_count": metadata.get("request_count", 1),
            "bytes_received": len(result.raw_bytes),
            "normalization_kind": "deepseek_bridge" if isinstance(self.adapters[source_id], DeepSeekBridge) else "registered_adapter"}

    def _call(self, job_id, source_id, operation, parameters, *, ordinal=0):
        self._write()
        plan = self.archive.plan(job_id)
        if source_id not in plan["source_ids"]:
            raise IntelligenceError("POLICY_DENIED", "Source is not admitted by the frozen plan")
        spec = self.registry.get(source_id)
        adapter = self.adapters.get(source_id)
        if adapter is None:
            raise SourceError("SOURCE_AUTH_REQUIRED", delivery="not_sent")
        deadline = self._deadline(job_id)
        if time.monotonic() >= deadline:
            raise IntelligenceError("BUDGET_EXHAUSTED", "Acquisition deadline exhausted")
        status = self.status(job_id)
        remaining = plan["limits"]["requests"] - status["usage"]["requests"]
        remaining_bytes = plan["limits"]["bytes"] - status["usage"]["bytes"]
        if remaining_bytes <= 0:
            raise IntelligenceError("BUDGET_EXHAUSTED", "Acquisition byte quota exhausted")
        reserved = 2 if isinstance(adapter, DeepSeekBridge) and operation == "search" else min(6, remaining)
        if reserved < 1 or isinstance(adapter, DeepSeekBridge) and remaining < reserved:
            raise IntelligenceError("BUDGET_EXHAUSTED", "Acquisition request quota exhausted")
        request = {"parameters": parameters, "reserved_requests": reserved}
        attempt_id = self.archive.begin_attempt(job_id, source_id, operation, request, ordinal=ordinal)
        inputs = {"schema": "tiger.intelligence-source-request/v1", "job_id": job_id,
            "attempt_id": attempt_id, "source_id": source_id, "source_version": spec.version,
            "operation": operation, "parameters": parameters, "request_sha256": digest(parameters),
            "registry_sha256": self.registry.public_manifest()["sha256"]}
        delay = max(0.0, self._next_source.get(source_id, 0.0) - time.monotonic())
        if delay:
            if time.monotonic() + delay >= deadline:
                error = SourceError("DEADLINE_EXCEEDED", delivery="not_sent")
                self.archive.settle_attempt(job_id, attempt_id, "failed", {"error": error.to_dict(),
                    "bytes_received": 0, "request_count": 0, "usage": {"state": "not_sent", "requires_reconciliation": False}})
                raise error
            self.sleeper(delay)
        transport = getattr(adapter, "transport", None)
        transport_before = (getattr(transport, "total_request_count", 0), getattr(transport, "total_bytes_received", 0))
        def transport_usage():
            return {"request_count": getattr(transport, "total_request_count", 0) - transport_before[0],
                    "bytes_received": getattr(transport, "total_bytes_received", 0) - transport_before[1]}
        previous = None
        if transport is not None:
            previous = (getattr(transport, "max_bytes", plan["limits"]["bytes"]),
                        getattr(transport, "deadline", None), getattr(transport, "max_hops", 6))
            transport.max_bytes = remaining_bytes
            transport.deadline, transport.max_hops = deadline, reserved
        native_meter = None
        def invoke():
            nonlocal native_meter
            budget = getattr(adapter.client, "native_io_budget", None) if isinstance(adapter, DeepSeekBridge) else None
            context = budget(max_bytes=remaining_bytes, deadline=deadline) if callable(budget) else nullcontext(None)
            with context as meter:
                native_meter = meter
                if operation == "search":
                    result = adapter.search(_parameters(parameters))
                else:
                    result = adapter.fetch(parameters["url"])
            observation = self._observe_result(source_id, operation, parameters, result)
            if native_meter and native_meter.get("transport_observed"):
                observation["bytes_received"] = native_meter["bytes_received"]
                observation["request_count"] = native_meter["request_count"]
            elif transport_usage()["request_count"]:
                observation.update(transport_usage())
            return observation
        try:
            if isinstance(adapter, DeepSeekBridge):
                # Its admitted client already records network I/O. Finish that
                # call before appending this projection; nested handles would
                # complete out of order in the existing execution journal.
                observation = invoke()
                observation = self.observe_io("intelligence.source.request", inputs, lambda: observation)
            else:
                observation = self.observe_io("intelligence.source.request", inputs, invoke)
        except SourceError as error:
            if native_meter and (native_meter.get("transport_observed") or native_meter.get("limit_reason")):
                error.bytes_received = native_meter["bytes_received"]
                error.details.update(bytes_received=error.bytes_received, request_count=native_meter["request_count"])
                if native_meter.get("limit_reason"):
                    error.code = "BUDGET_EXHAUSTED" if native_meter["limit_reason"] == "bytes" else "DEADLINE_EXCEEDED"
                if native_meter["request_count"] == 0:
                    error.delivery = "not_sent"
                    error.details["delivery"] = "not_sent"
            elif transport_usage()["request_count"]:
                error.bytes_received = transport_usage()["bytes_received"]
                error.details.update(transport_usage())
            count = error.details.get("request_count")
            if count is None:
                count = 0 if error.delivery == "not_sent" else reserved if isinstance(adapter, DeepSeekBridge) else 1
            outcome = "unknown" if error.delivery == "unknown" else "failed"
            result = {"error": error.to_dict(), "bytes_received": error.bytes_received, "request_count": count,
                "usage": {"state": "not_sent" if error.delivery == "not_sent" else "unknown",
                          "requires_reconciliation": error.delivery != "not_sent"}}
            self.archive.settle_attempt(job_id, attempt_id, outcome, result)
            error.details["attempt_id"] = attempt_id
            raise
        except BaseException:
            self.archive.settle_attempt(job_id, attempt_id, "unknown", {"error": {"code": "OBSERVATION_FAILED"},
                "bytes_received": native_meter["bytes_received"] if native_meter else transport_usage()["bytes_received"],
                "request_count": native_meter["request_count"] if native_meter and native_meter.get("transport_observed") else transport_usage()["request_count"] or reserved,
                "usage": {"state": "unknown", "requires_reconciliation": True}})
            raise
        finally:
            self._next_source[source_id] = time.monotonic() + spec.minimum_interval_seconds
            if transport is not None and previous is not None:
                transport.max_bytes, transport.deadline, transport.max_hops = previous
        hits = observation["normalized"].get("hits") if operation == "search" else None
        result = {"observation": observation, "bytes_received": observation["bytes_received"],
                  "request_count": observation["request_count"], "usage": observation["usage"]}
        self.archive.settle_attempt(job_id, attempt_id, "empty" if hits == [] else "completed", result)
        return attempt_id, observation

    def _authority(self, source_id, hit, text_hash, *, fetch=None, discovery=None):
        spec = self.registry.get(source_id)
        candidate = hit.get("raw_metadata", {}).get("authority", {})
        if spec.kind == "search" or not isinstance(candidate, dict) or candidate.get("scope") not in spec.authority_scopes:
            candidate = {"scope": "discovery", "series_ids": [], "proof_type": "unverified", "proof_ref": {}}
        value = json.loads(canonical(candidate))
        proof = value.setdefault("proof_ref", {})
        proof.update(source_id=source_id, content_sha256=text_hash)
        if discovery:
            proof.setdefault("source_url", discovery["response_url"])
            proof.setdefault("response_sha256", discovery["transport_sha256"])
            proof["discovery_response_ref"] = discovery["raw_response_ref"]
        if fetch:
            proof["source_url"] = fetch["normalized"]["final_url"]
            proof["response_sha256"] = fetch["transport_sha256"]
            proof["original_response_ref"] = fetch["raw_response_ref"]
        return value

    def _document(self, source_id, hit, discovery, *, hit_index=0, fetch=None,
                  discovery_attempt=None, fetch_attempt=None):
        value = fetch["normalized"] if fetch else hit
        text = value.get("text") or (hit.get("snippet", "") if not fetch else "")
        text = safe_value(text, self._secrets())
        url = value.get("final_url") or value.get("url") or hit["url"]
        canonicalized = canonical_url(url)
        original = hit["url"]
        raw_ref = (fetch or discovery)["raw_response_ref"]
        text_ref = self.archive.put_blob(text.encode(), "text/plain; charset=utf-8")
        when = source_time(timestamp(self.clock()), evidence={"kind": "local_observation"})
        published = value.get("published") or hit.get("published") or source_time(None)
        if fetch and published.get("value") is None and hit.get("published", {}).get("value") is not None:
            published = hit["published"]
        if self.registry.get(source_id).kind == "sec_submissions":
            published = hit.get("published") or source_time(None)
        kind = value.get("text_kind", "snippet")
        completeness = value.get("completeness", "unknown")
        if (fetch or discovery).get("raw_redacted"):
            completeness = "partial"
        version = digest({"canonical_url": canonicalized, "source_id": source_id,
            "text_sha256": text_ref["sha256"], "text_kind": kind, "published": published,
            "updated": value.get("updated"), "completeness": completeness})
        doc_id = "intel_" + hashlib.sha256(canonicalized.encode()).hexdigest()[:24]
        doc_version_id = "docver_" + digest({"doc_id": doc_id, "version": version})[:32]
        authority_source = source_id
        authority = self._authority(authority_source, hit, text_ref["sha256"], fetch=fetch, discovery=discovery)
        normalization = {"kind": (fetch or discovery)["normalization_kind"], "source_id": source_id,
            "operation": "fetch" if fetch else "search", "parameters": {"url": original} if fetch else discovery.get("parameters", {}),
            "hit_index": hit_index, "raw_response_ref": raw_ref,
            "response_url": (fetch or discovery)["response_url"], "http_headers": (fetch or discovery)["http_headers"],
            "received_at": (fetch or discovery)["received_at"],
            "raw_redacted": (fetch or discovery).get("raw_redacted", False),
            "transport_sha256": (fetch or discovery)["transport_sha256"],
            "request_count": (fetch or discovery)["request_count"],
            "actual_request": (fetch or discovery)["actual_request"],
            "discovery": {"source_id": discovery["source_id"], "parameters": discovery.get("parameters", {}),
                "raw_response_ref": discovery["raw_response_ref"], "response_url": discovery["response_url"],
                "raw_redacted": discovery.get("raw_redacted", False),
                "transport_sha256": discovery["transport_sha256"],
                "request_count": discovery["request_count"],
                "http_headers": discovery["http_headers"], "received_at": discovery["received_at"], "hit_index": hit_index}}
        normalization["discovery"]["normalization_kind"] = discovery["normalization_kind"]
        retrieved = source_time((fetch or discovery)["received_at"], evidence={"kind": "source_transport"})
        record = {"schema": DOCUMENT_SCHEMA, "id": doc_version_id, "doc_id": doc_id, "version": version,
            "canonical_url": canonicalized, "original_url": original, "final_url": url,
            "title": value.get("title") or hit.get("title", ""), "publisher_id": urlsplit(url).hostname,
            "origin_cluster": urlsplit(url).hostname, "source_id": source_id,
            "source_version": self.registry.get(source_id).version, "raw_blob": raw_ref, "raw_ref": raw_ref,
            "text_blob": text_ref, "text": text, "text_kind": kind, "completeness": completeness,
            "times": {"published": published, "updated": value.get("updated") or source_time(None),
                "event": hit.get("raw_metadata", {}).get("event_time") or source_time(None),
                "first_seen": when, "retrieved": retrieved},
            "attachments": value.get("attachments", []), "authority": authority,
            "normalization": normalization, "source_attempt_refs": [x for x in [discovery_attempt, fetch_attempt] if x],
            "source_metadata": hit.get("raw_metadata", {}), "extraction_metadata": value.get("metadata", {})}
        return safe_value(record, self._secrets())

    def _directory(self, job_id):
        docs = self.archive.documents(job_id)
        return next((d for d in reversed(docs) if d["text_kind"] == "identity_directory"), None)

    def _enrich_request(self, job_id, source_id, request):
        if self.registry.get(source_id).kind != "sec_submissions":
            return request
        directory = self._directory(job_id)
        if directory:
            entries = directory["source_metadata"].get("identity_directory", {})
            row = entries.get(request.context.get("ticker"))
            if row:
                return replace(request, context={**request.context, "cik": row["cik"],
                    "issuer_directory_ref": {"document_id": directory["id"],
                        "content_sha256": directory["text_blob"]["sha256"],
                        "raw_response_ref": directory["raw_blob"], "ticker": request.context["ticker"],
                        "text_blob": directory["text_blob"], "source_id": directory["source_id"],
                        "normalization": directory["normalization"],
                        "cik": row["cik"], "first_seen_at": directory["times"]["first_seen"]["value"]}})
        return request

    def _mark(self, job_id, cells, *, status, reason, attempt_refs=(), document_ids=()):
        for cell_id in cells:
            self.archive.gap(job_id, {"cell_id": cell_id, "status": status, "reason": reason,
                "attempt_refs": list(attempt_refs), "document_ids": list(document_ids),
                **({"diagnostic_kind": "source"} if reason not in {"SEMANTIC_ASSESSMENT_REQUIRED",
                    "HOLDINGS_NOT_CONNECTED", "HOLDING_OUTSIDE_REVIEWED_ROSTER", "PENDING_OUTSIDE_REVIEWED_ROSTER"} else {})})

    def _fetch(self, job_id, discovery_source, hit, discovery, index, search_attempt, request, cell_ids):
        plan = self.archive.plan(job_id)
        original_spec = self.registry.get(discovery_source)
        fetch_source = discovery_source if original_spec.kind not in {"search", "web_fetch"} else "web_page" if "web_page" in plan["source_ids"] else discovery_source
        if "fetch" not in self.registry.get(fetch_source).capabilities:
            return None
        attempt, observation = self._call(job_id, fetch_source, "fetch", {"url": hit["url"]})
        observation["parameters"] = {"url": hit["url"]}
        # Authority belongs to the registered direct source, not to a general
        # search provider. A web discovery/fetch remains discovery authority.
        authority_source = discovery_source if original_spec.kind != "search" else fetch_source
        document = self._document(authority_source, hit, discovery, hit_index=index,
            fetch=observation, discovery_attempt=search_attempt, fetch_attempt=attempt)
        stored = self.archive.put_document(job_id, document, attempt_id=attempt, rank=index,
            targets=request.targets, entities=request.entities, hit=hit)
        if observation["normalized"].get("metadata", {}).get("extraction_error"):
            self._mark(job_id, cell_ids, status="unverifiable", reason="TEXT_EXTRACTION_FAILED",
                       attempt_refs=[attempt], document_ids=[stored["id"]])
        for attachment in document["attachments"]:
            # Attachments are bounded by the same explicit tool/byte/deadline
            # quota. Their absence stays a visible gap; no text is truncated.
            try:
                extra_attempt, extra_observation = self._call(job_id, fetch_source, "fetch", {"url": attachment["url"]})
                extra_hit = {**hit, "url": attachment["url"], "title": attachment.get("label") or hit["title"]}
                if original_spec.kind != "sec_submissions":
                    extra_hit["published"] = source_time(None, evidence={"kind": "attachment_publication_unknown",
                        "parent_document_id": stored["id"]})
                extra = self._document(authority_source, extra_hit, discovery, hit_index=index,
                    fetch=extra_observation, discovery_attempt=search_attempt, fetch_attempt=extra_attempt)
                extra["parent_document_id"] = stored["id"]
                extra["normalization"]["attachment"] = {"url": attachment["url"], "label": attachment.get("label", ""),
                    "parent_document_id": stored["id"], "parent_normalization": stored["normalization"]}
                self.archive.put_document(job_id, extra, attempt_id=extra_attempt, rank=index,
                    targets=request.targets, entities=request.entities, hit={"attachment_of": stored["id"]})
            except (SourceError, IntelligenceError) as error:
                if error.code in {"ARCHIVE_UNAVAILABLE", "HASH_MISMATCH", "FROZEN_BUNDLE"}:
                    raise
                self._mark(job_id, cell_ids, status="unverifiable", reason="ATTACHMENT_" + error.code,
                    document_ids=[stored["id"]])
        return stored

    def _query(self, job_id, source_id, request, cell_ids):
        spec = self.registry.get(source_id)
        request = self._enrich_request(job_id, source_id, request)
        collected, attempts = [], []
        compiled_requests = compile_requests(source_id, request)
        for compiled in compiled_requests:
            observation = None
            for ordinal in range(spec.max_retries + 1):
                try:
                    attempt, observation = self._call(job_id, source_id, "search", compiled.to_dict(), ordinal=ordinal)
                    attempts.append(attempt)
                    observation["parameters"] = compiled.to_dict()
                    break
                except SourceError as error:
                    if error.details.get("attempt_id"):
                        attempts.append(error.details["attempt_id"])
                    retryable = error.code in {"RATE_LIMITED", "SOURCE_UNAVAILABLE", "SOURCE_TIMEOUT"}
                    delay = error.retry_after if error.retry_after is not None else float(2**ordinal)
                    if not retryable or ordinal >= spec.max_retries or time.monotonic() + delay >= self._deadline(job_id):
                        self._mark(job_id, cell_ids, status="unverifiable", reason=error.code, attempt_refs=attempts)
                        break
                    self.sleeper(delay)
            if observation is None:
                continue
            if spec.kind == "sec_submissions" and not compiled.context.get("submissions_file"):
                metadata = observation["normalized"].get("response_metadata", {})
                for older in metadata.get("older_files", []):
                    if (not isinstance(older, dict) or re.fullmatch(r"CIK" + str(compiled.context.get("cik", "")).zfill(10)
                            + r"-submissions-\d{3,}\.json", str(older.get("name", ""))) is None):
                        self._mark(job_id, cell_ids, status="unverifiable", reason="SUBMISSIONS_FILE_IDENTITY_INVALID", attempt_refs=attempts)
                        continue
                    if (compiled.start_date and older.get("filingTo", "9999") < compiled.start_date
                            or compiled.end_date and older.get("filingFrom", "0000") > compiled.end_date):
                        continue
                    parent = {"raw_response_ref": observation["raw_response_ref"],
                        "response_url": observation["response_url"], "transport_sha256": observation["transport_sha256"],
                        "cik": str(compiled.context["cik"]).zfill(10), "name": older["name"],
                        "issuer_name": metadata.get("response", {}).get("name", "")}
                    compiled_requests.append(replace(compiled, context={**compiled.context,
                        "issuer_name": parent["issuer_name"], "submissions_file": {"name": older["name"], "parent_ref": parent}}))
            hits = observation["normalized"]["hits"]
            if not hits:
                self._mark(job_id, cell_ids, status="missing", reason="NO_RESULTS_NOT_NO_EVENT", attempt_refs=attempts)
                continue
            fetch_failed = False
            for index, hit in enumerate(hits):
                document = self._document(source_id, hit, observation, hit_index=index, discovery_attempt=attempts[-1])
                stored = self.archive.put_document(job_id, document, attempt_id=attempts[-1], rank=index,
                    targets=compiled.targets, entities=compiled.entities, hit=hit)
                collected.append(stored["id"])
                if hit.get("text_kind") not in {"data", "calendar", "identity_directory"}:
                    try:
                        full = self._fetch(job_id, source_id, hit, observation, index, attempts[-1], compiled, cell_ids)
                        if full:
                            collected.append(full["id"])
                    except (SourceError, IntelligenceError) as error:
                        if error.code in {"ARCHIVE_UNAVAILABLE", "HASH_MISMATCH", "FROZEN_BUNDLE"}:
                            raise
                        self._mark(job_id, cell_ids, status="unverifiable", reason=error.code,
                            attempt_refs=attempts, document_ids=[stored["id"]])
                        fetch_failed = True
            if not fetch_failed:
                # Preserve extraction/attachment failures written by _fetch;
                # source discovery alone cannot erase those material gaps.
                latest = {row["cell_id"]: row for row in self.status(job_id)["gaps"]}
                pending = [cell for cell in cell_ids if latest.get(cell, {}).get("status") != "unverifiable"]
                self._mark(job_id, pending, status="pending", reason="SEMANTIC_ASSESSMENT_REQUIRED",
                           attempt_refs=attempts, document_ids=collected)
        return collected

    def collect(self, job_id):
        self._write()
        plan = self.archive.plan(job_id)
        self.archive.set_state(job_id, "running")
        for entry in plan["query_plan"]:
            request_hash = digest(entry["request"])
            completed = [row for row in self.archive.attempts(job_id) if row["source_id"] == entry["source_id"]
                         and row["operation"] == "search" and row["request"].get("parameters", {}).get("context", {}).get("plan_request_hash") == request_hash]
            if completed:
                continue
            request = _parameters(entry["request"])
            request = replace(request, context={**request.context, "plan_request_hash": request_hash})
            try:
                self._query(job_id, entry["source_id"], request, entry["cell_ids"])
            except IntelligenceError as error:
                if error.code not in {"BUDGET_EXHAUSTED", "CONTRACT_INVALID", "SOURCE_AUTH_REQUIRED", "SOURCE_IDENTITY_REQUIRED"}:
                    raise
                self._mark(job_id, entry["cell_ids"], status="unverifiable", reason=error.code)
                if error.code == "BUDGET_EXHAUSTED":
                    break
        for cell in plan["coverage_cells"]:
            if cell["material_type"] == "connection":
                outside = cell.get("connection_reason") in {"HOLDING_OUTSIDE_REVIEWED_ROSTER", "PENDING_OUTSIDE_REVIEWED_ROSTER"}
                self._mark(job_id, [cell["cell_id"]], status="unverifiable" if outside else "not_connected",
                    reason=cell.get("connection_reason", "HOLDINGS_NOT_CONNECTED"))
        self.archive.set_state(job_id, "partial")
        return self._acquired(job_id)

    def _acquired(self, job_id):
        return {**self.status(job_id), "documents": self.archive.documents(job_id),
                "entity_registry": self.entity_registry(job_id), "coverage": self.archive.assessment(job_id)["coverage"]}

    def entity_registry(self, job_id):
        plan = self.archive.plan(job_id)
        result = {"securities": plan["universe"], "entities": {}}
        for key, value in plan["entity_registry"].items():
            ids = value.get("identifiers", [])
            if isinstance(ids, list):
                grouped = {}
                for item in ids:
                    grouped.setdefault(item["scheme"], []).append(item["value"])
                ids = grouped
            result["entities"][key] = {**value, "identifiers": ids}
        directory = self._directory(job_id)
        if directory:
            for symbol, row in directory["source_metadata"].get("identity_directory", {}).items():
                if symbol not in plan["universe"]:
                    continue
                key = "issuer:" + row["cik"]
                result["entities"][key] = {"type": "issuer", "name": row["name"],
                    "aliases": [row["name"], symbol], "identifiers": {"ticker": [symbol], "cik": [row["cik"]]},
                    "security_ids": [plan["universe"][symbol]], "source_ref": directory["id"]}
        return result

    def record_assessment(self, job_id, claims, coverage_rows):
        self._write()
        if not isinstance(claims, list) or not isinstance(coverage_rows, list):
            raise IntelligenceError("CONTRACT_INVALID", "Claims and coverage must be arrays")
        plan = self.archive.plan(job_id)
        documents = {d["id"]: d for d in self.archive.documents(job_id)}
        document_ids = set(documents)
        raw_ids = {d["doc_id"] for d in documents.values()} | {
            "ds_" + digest({"id": d["id"], "version": d["version"]})[:24] for d in documents.values()}
        attempts = {a["attempt_id"] for a in self.archive.attempts(job_id)}
        baseline_attempts = {}
        admitted_baselines = {ref["manifest_sha256"] for ref in plan["baseline_refs"] if isinstance(ref, dict) and "manifest_sha256" in ref}
        for event in self.archive.events(job_id, kind="baseline_import"):
            receipt = event["payload"]
            if receipt.get("manifest_sha256") not in admitted_baselines:
                continue
            manifest = receipt.get("manifest")
            if (not isinstance(manifest, dict) or manifest.get("schema") != "tiger.intelligence-baseline/v1"
                    or manifest.get("manifest_sha256") != receipt["manifest_sha256"]
                    or digest({key: value for key, value in manifest.items() if key != "manifest_sha256"}) != receipt["manifest_sha256"]
                    or manifest.get("tenant") != self.archive.tenant or manifest.get("as_of") != plan["as_of"]
                    or manifest.get("archive_id") != receipt.get("archive_id") or manifest.get("event_head") != receipt.get("event_head")):
                raise IntelligenceError("HASH_MISMATCH", "Completed baseline import is not bound to its original admitted manifest")
            eligible_versions = {version["id"]: version for version in manifest["versions"]}
            for version in receipt.get("versions", []):
                if eligible_versions.get(version.get("id")) != version:
                    raise IntelligenceError("HASH_MISMATCH", "Imported baseline version is absent from the admitted manifest")
                document = documents.get(version.get("id"))
                if document is None:
                    raise IntelligenceError("HASH_MISMATCH", "Completed baseline import document is missing")
                original = {key: value for key, value in document.items()
                    if key not in {"target_ids", "requested_entity_ids", "query_hit_ids"}}
                if digest(original) != version.get("document_sha256"):
                    raise IntelligenceError("HASH_MISMATCH", "Imported baseline document differs from its admitted receipt")
                baseline_attempts[document["id"]] = set(document["source_attempt_refs"])
        aliases = {}
        for document in documents.values():
            for alias in (document["id"], document["doc_id"],
                    "ds_" + digest({"id": document["id"], "version": document["version"]})[:24]):
                aliases.setdefault(alias, set()).add(document["id"])
        allowed_targets = {c["target_id"] for c in plan["coverage_cells"]}
        for claim in claims:
            if (not isinstance(claim, dict) or not isinstance(claim.get("target_ids", []), list)
                    or any(not isinstance(value, str) for value in claim.get("target_ids", []))
                    or set(claim.get("target_ids", [])) - allowed_targets):
                raise IntelligenceError("CONTRACT_INVALID", "Claim target is outside the plan")
            refs = claim.get("document_ids", claim.get("evidence_refs", []))
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
                raise IntelligenceError("CONTRACT_INVALID", "Claims require typed source evidence references")
            if set(refs) - document_ids - raw_ids:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Claim cites an unknown document")
        normalized = []
        for row in coverage_rows:
            if not isinstance(row, dict):
                raise IntelligenceError("CONTRACT_INVALID", "Invalid coverage row")
            if {"diagnostic_kind", "source_diagnostics", "source_reason"}.intersection(row):
                raise IntelligenceError("CONTRACT_INVALID", "Semantic assessments cannot supply source diagnostics")
            matched = [c for c in plan["coverage_cells"] if c["cell_id"] == row.get("cell_id") or not row.get("cell_id") and c["target_id"] == row.get("target_id")]
            if not matched:
                raise IntelligenceError("CONTRACT_INVALID", "Assessment target is outside the plan")
            refs = row.get("document_ids", row.get("evidence_refs", row.get("evidence_ids", [])))
            attempt_refs = row.get("attempt_refs", [])
            if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                    or not isinstance(attempt_refs, list) or any(not isinstance(ref, str) for ref in attempt_refs)):
                raise IntelligenceError("CONTRACT_INVALID", "Assessment references must be typed arrays")
            cited_versions = {next(iter(aliases[ref])) for ref in refs if ref in aliases and len(aliases[ref]) == 1}
            allowed_attempts = attempts | {attempt for version in cited_versions for attempt in baseline_attempts.get(version, ())}
            if set(refs) - document_ids - raw_ids or set(attempt_refs) - allowed_attempts:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Assessment cites unavailable evidence")
            status = {"assessed": "present", "insufficient_evidence": "missing", "conflicted": "conflicting"}.get(row.get("status"), row.get("status"))
            if status not in {"present", "missing", "conflicting", "unverifiable", "not_connected", "pending"}:
                raise IntelligenceError("CONTRACT_INVALID", "Invalid assessment status")
            if status == "present" and not refs:
                raise IntelligenceError("CONTRACT_INVALID", "Present coverage requires source evidence")
            for cell in matched:
                value = {**row, "cell_id": cell["cell_id"], "target_id": cell["target_id"],
                    "status": status, "document_ids": refs, "attempt_refs": attempt_refs}
                normalized.append(value)
                self.archive.gap(job_id, {**value, "reason": row.get("reason", "DETERMINISTIC_ASSESSMENT")})
        assessment_id = self.archive.record_assessment(job_id, claims, normalized)
        return {"assessment_id": assessment_id, "revision": self.status(job_id)["revision"], "coverage": normalized}

    def research(self, job_id, gap_ids=None, replay_key=None):
        self._write()
        plan = self.archive.plan(job_id)
        current = self.status(job_id)
        if current["bundle"]:
            raise IntelligenceError("FROZEN_BUNDLE", "Frozen jobs cannot start research")
        previous = self.archive.events(job_id, kind="research_started")
        if replay_key and any(row["payload"].get("replay_key") == replay_key for row in previous):
            return self._acquired(job_id)
        if len(previous) >= plan["limits"]["rounds"] - 1:
            raise IntelligenceError("BUDGET_EXHAUSTED", "Research round quota exhausted")
        gaps = [g for g in current["gaps"] if g["status"] != "present"]
        if gap_ids is not None:
            if not isinstance(gap_ids, list) or any(not isinstance(value, str) for value in gap_ids):
                raise IntelligenceError("CONTRACT_INVALID", "Research gap ids must be a typed array")
            unknown = set(gap_ids) - {g["cell_id"] for g in gaps}
            if unknown:
                raise IntelligenceError("CONTRACT_INVALID", "Research gap is not unresolved")
            gaps = [g for g in gaps if g["cell_id"] in gap_ids]
        providers = [s for s in plan["source_ids"] if self.registry.get(s).kind == "search"]
        self.archive.append(job_id, "research_started", {"round": len(previous) + 2,
            "gap_ids": [g["cell_id"] for g in gaps], "replay_key": replay_key})
        cells = {c["cell_id"]: c for c in plan["coverage_cells"]}
        if not providers:
            affected = [gap for gap in gaps if cells[gap["cell_id"]]["material_type"] != "connection"]
            self.archive.append(job_id, "research_unavailable", {"round": len(previous) + 2,
                "reason": "NO_REGISTERED_DISCOVERY_PROVIDER", "code": "SOURCE_AUTH_REQUIRED",
                "gap_ids": [gap["cell_id"] for gap in affected], "request_count": 0, "bytes_received": 0})
            for gap in affected:
                if gap["status"] != "conflicting":
                    self._mark(job_id, [gap["cell_id"]], status="unverifiable",
                        reason="NO_REGISTERED_DISCOVERY_PROVIDER", attempt_refs=gap.get("attempt_refs", []),
                        document_ids=gap.get("document_ids", []))
            return self._acquired(job_id)
        for gap in sorted(gaps, key=lambda g: not g["required"]):
            cell = cells[gap["cell_id"]]
            if cell["material_type"] == "connection":
                continue
            query = cell["query"] + " primary source correction withdrawal contrary evidence"
            request = SearchRequest(query, count=10,
                start_date=(aware(plan["as_of"]).date() - timedelta(days=cell["window_days"])).isoformat(),
                end_date=aware(plan["as_of"]).date().isoformat(), topic="news", targets=(cell["target_id"],),
                entities=(cell["entity_id"],) if cell["entity_id"] else (), context={"research_round": len(previous) + 2})
            provider = providers[min(len(previous) + 1, len(providers) - 1)]
            try:
                self._query(job_id, provider, request, [cell["cell_id"]])
            except IntelligenceError as error:
                if error.code != "BUDGET_EXHAUSTED":
                    raise
                self._mark(job_id, [cell["cell_id"]], status="unverifiable", reason=error.code)
                break
        return self._acquired(job_id)

    def freeze(self, job_id, *, expected_plan_hash, expected_revision, expected_frontier_hash):
        self._write()
        return self.archive.freeze(job_id, expected_plan_hash=expected_plan_hash,
            expected_revision=expected_revision, expected_frontier_hash=expected_frontier_hash,
            source_manifest=self.registry.public_manifest(),
            baseline_refs=[row["payload"] for row in self.archive.events(job_id, kind="baseline_import")])

    def archive_versions(self, as_of, entities=()):
        return self.archive.archive_versions(as_of, entities)

    def import_baseline(self, job_id, baseline_archive_root, *, version_ids, expected_manifest_hash):
        self._write()
        plan = self.archive.plan(job_id)
        origin = Archive(baseline_archive_root, tenant=self.archive.tenant, readonly=True)
        manifest = origin.archive_versions(plan["as_of"])
        if manifest["manifest_sha256"] != expected_manifest_hash:
            raise IntelligenceError("PLAN_CONFLICT", "Baseline manifest changed")
        if not any(ref.get("manifest_sha256") == expected_manifest_hash for ref in plan["baseline_refs"]):
            raise IntelligenceError("PLAN_CONFLICT", "Baseline was not bound to this plan")
        eligible = {row["id"]: row for row in manifest["versions"]}
        if set(version_ids) - eligible.keys():
            raise IntelligenceError("TIME_UNVERIFIED", "Baseline version was not observed before cutoff")
        imported = []
        for version_id in version_ids:
            doc = origin.document(version_id)
            if doc["source_id"] not in plan["source_ids"] or doc["source_version"] != self.registry.get(doc["source_id"]).version:
                raise IntelligenceError("PLAN_CONFLICT", "Baseline source contract changed")
            from .replay import reconstruct_document
            reconstruct_document(doc, origin.read_blob, self.registry.public_manifest())
            for ref in _blob_refs(doc):
                self.archive.put_blob(origin.read_blob(ref), ref.get("media_type", "application/octet-stream"))
            self.archive.import_document(job_id, doc, manifest, eligible[version_id]["entity_ids"])
            imported.append(version_id)
        receipt = {"archive_id": manifest["archive_id"], "manifest_sha256": expected_manifest_hash,
            "manifest": manifest,
            "event_head": manifest["event_head"], "versions": [eligible[x] for x in imported],
            "imported_at": timestamp(self.clock()), "reused_semantic_annotations": False,
            "reused_search_results": False}
        self.archive.append(job_id, "baseline_import", receipt)
        return receipt

    def evidence(self, bundle_id=None, *, document_id=None, claim_id=None, doc_id=None, version=None, job_id=None):
        if document_id:
            document = self.archive.document(document_id)
            return {"document": document, "raw_ref": document["raw_blob"], "text_ref": document["text_blob"],
                    "normalization": document["normalization"]}
        if job_id:
            return self._acquired(job_id)
        if not bundle_id:
            raise IntelligenceError("CONTRACT_INVALID", "Evidence needs a bundle, job or document id")
        bundle = self.archive.bundle(bundle_id)
        if claim_id:
            claims = [c for c in bundle["claims"] if c.get("id", c.get("claim_id")) == claim_id]
            if len(claims) != 1:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Claim is unavailable")
            return {"bundle_id": bundle_id, "claim": claims[0], "documents": bundle["documents"]}
        if doc_id:
            docs = [d for d in bundle["documents"] if d["doc_id"] == doc_id and (version is None or d["version"] == version)]
            if not docs:
                raise IntelligenceError("EVIDENCE_NOT_FOUND", "Document version is unavailable")
            return {"bundle_id": bundle_id, "documents": docs}
        return bundle


def project_to_rawdocuments(documents, *, include_structural=False):
    """Project a declared evidence subset, preserving exact normalized text/hash."""
    from ..search.models import RawDocument

    chosen = {}
    for doc in documents:
        if not include_structural and doc["text_kind"] == "identity_directory":
            continue
        if not doc["text"].strip():
            continue
        chosen[doc["id"]] = doc
    result = []
    for doc in chosen.values():
        when = doc["times"]["published"]
        result.append(RawDocument(doc_id="ds_" + digest({"id": doc["id"], "version": doc["version"]})[:24], source="intelligence:" + doc["source_id"],
            title=doc["title"], url=doc["final_url"], content=doc["text"], published=when.get("value") or "",
            fetched_at=aware(doc["times"]["first_seen"]["value"]).timestamp(),
            meta={"source_policy_id": doc["source_id"], "intelligence_document_id": doc["id"],
                "intelligence_version": doc["version"], "text_kind": doc["text_kind"],
                "intelligence_document_base_id": doc["doc_id"],
                "completeness": doc["completeness"], "source_times": doc["times"],
                "authority": doc["authority"], "target_ids": doc.get("target_ids", []),
                "normalization": doc["normalization"], "source_attempt_refs": doc["source_attempt_refs"]}))
    return result
