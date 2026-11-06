"""Offline parser replay from frozen request/response bytes; never opens a socket."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from urllib.parse import urlsplit

from .archive import Archive
from .contracts import (FetchResult, IntelligenceError, SearchRequest, SearchResult,
                        canonical, canonical_url, digest, safe_value, source_time)
from .registry import SourceRegistry, SourceSpec
from .transport import HttpResponse


class _FrozenTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.max_bytes, self.max_hops, self.deadline = len(response.body) + 1, 1, None

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url,
            "params": {k: v for k, v in (kwargs.get("params") or {}).items() if k != "api_key"},
            "json_body": kwargs.get("json_body")})
        return self.response


def replay_observation(spec, operation, parameters, raw_response, *, response_url,
                       headers=None, received_at, normalization_kind="registered_adapter",
                       transport_sha256=None, request_count=1, parse_failure=None):
    """Re-run the actual adapter parser against exact frozen bytes and parameters.

    A compatibility Bridge stores a sanitized projection with lower evidentiary
    scope. Its replay does not invent unavailable upstream wire metadata.
    """
    if isinstance(spec, dict):
        spec = SourceSpec(**spec)
    if not isinstance(spec, SourceSpec) or not isinstance(raw_response, bytes):
        raise IntelligenceError("CONTRACT_INVALID", "Replay needs a registered source and bytes")
    if normalization_kind == "deepseek_bridge":
        try:
            payload = json.loads(raw_response)
        except (ValueError, UnicodeError):
            raise IntelligenceError("HASH_MISMATCH", "Bridge projection cannot be decoded") from None
        if operation == "search":
            if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
                raise IntelligenceError("HASH_MISMATCH", "Bridge search projection differs")
            return SearchResult(payload["hits"], payload["request"], payload.get("response_metadata", {}),
                                payload.get("usage", {}), payload.get("warnings", []), raw_response)
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise IntelligenceError("HASH_MISMATCH", "Bridge Fetch projection differs")
        return FetchResult(parameters["url"], payload.get("url", parameters["url"]), payload.get("title", ""),
            payload["content"], raw_response, "application/json", "provider_markdown", "unknown",
            source_time(payload.get("published")))
    response = HttpResponse(response_url, 200, headers or {}, raw_response, received_at,
                            transport_sha256 or hashlib.sha256(raw_response).hexdigest(), request_count, parse_failure)
    transport = _FrozenTransport(response)
    parser_spec = replace(spec, credential_env="TIGER_OFFLINE_REPLAY") if spec.kind in {"search", "fred"} else spec
    registry = SourceRegistry([parser_spec])
    env = {name: "replay-only-not-a-credential" for name in [parser_spec.credential_env, "TIGER_INTEL_SEC_USER_AGENT"] if name}
    parse_policy = parameters.get("_parse_policy", parameters.get("context", {}).get("_parse_policy"))
    adapter = registry.build_adapters(transport=transport, environment=env, parser_policy=parse_policy)[spec.source_id]
    if operation == "search":
        request = SearchRequest(**{k: tuple(v) if k in {"domains", "entities", "targets"} else v for k, v in parameters.items()})
        result = adapter.search(request)
    elif operation == "fetch":
        # Raw direct-page adapters normalize bytes rather than synthesizing a
        # second response. Provider Fetch endpoints parse their frozen JSON.
        result = adapter.fetch(parameters["url"])
    else:
        raise IntelligenceError("CONTRACT_INVALID", "Unsupported replay operation")
    if len(transport.requests) != 1:
        raise IntelligenceError("HASH_MISMATCH", "Replay parser attempted multiple responses")
    return result


def reconstruct_document(document, read_blob, source_manifest):
    """Reconstruct text, time and authority prerequisites; no saved verified flag."""
    specs = source_manifest.get("sources", {})
    recipe = document.get("normalization", {})
    source = specs.get(recipe.get("source_id"))
    if not source:
        raise IntelligenceError("HASH_MISMATCH", "Document source is not in the frozen registry")
    if document.get("source_id") != recipe.get("source_id") or document.get("source_version") != source.get("version"):
        raise IntelligenceError("HASH_MISMATCH", "Document source identity/version differs from normalization policy")
    contract_hash = recipe.get("source_contract_sha256")
    if contract_hash is not None and contract_hash != digest(source):
        raise IntelligenceError("HASH_MISMATCH", "Document registered source contract differs")
    if "material_types" in document and (not contract_hash
            or document.get("material_basis") != "registered_source_policy"
            or document["material_types"] != list(source.get("options", {}).get("material_types", []))):
        raise IntelligenceError("HASH_MISMATCH", "Document materials are not bound to registered source policy")
    try:
        discovery_recipe = recipe["discovery"]
        discovery_spec = specs[discovery_recipe["source_id"]]
        discovery = replay_observation(discovery_spec, "search", discovery_recipe["parameters"],
            read_blob(discovery_recipe["raw_response_ref"]), response_url=discovery_recipe["response_url"],
            headers=discovery_recipe["http_headers"], received_at=discovery_recipe["received_at"],
            normalization_kind=discovery_recipe.get("normalization_kind", "registered_adapter"),
            transport_sha256=discovery_recipe.get("transport_sha256"), request_count=discovery_recipe.get("request_count", 1),
            parse_failure=discovery_recipe.get("parse_failure"))
        hit = discovery.hits[recipe["hit_index"]]
        attachment = recipe.get("attachment")
        if attachment:
            parent = attachment["parent_normalization"]
            parent_source = specs[parent["source_id"]]
            parent_fetch = replay_observation(parent_source, parent.get("operation", "fetch"), parent["parameters"],
                read_blob(parent["raw_response_ref"]), response_url=parent["response_url"],
                headers=parent["http_headers"], received_at=parent["received_at"],
                normalization_kind=parent.get("kind", "registered_adapter"),
                transport_sha256=parent.get("transport_sha256"), request_count=parent.get("request_count", 1),
                parse_failure=parent.get("parse_failure"))
            parent_attachments = (parent_fetch.hits[parent["hit_index"]].get("attachments", [])
                                  if isinstance(parent_fetch, SearchResult) else parent_fetch.attachments)
            if not any(row["url"] == attachment["url"] and row.get("label", "") == attachment["label"] for row in parent_attachments):
                raise IntelligenceError("HASH_MISMATCH", "Attachment is not linked by the original parent bytes")
            hit = {**hit, "url": attachment["url"], "title": attachment["label"] or hit["title"]}
            if source["kind"] != "sec_submissions":
                hit["published"] = source_time(None, evidence={"kind": "attachment_publication_unknown",
                    "parent_document_id": attachment["parent_document_id"]})
        if recipe["operation"] == "fetch":
            fetched = replay_observation(source, "fetch", recipe["parameters"], read_blob(recipe["raw_response_ref"]),
                response_url=recipe["response_url"], headers=recipe["http_headers"], received_at=recipe["received_at"],
                normalization_kind=recipe.get("kind", "registered_adapter"),
                transport_sha256=recipe.get("transport_sha256"), request_count=recipe.get("request_count", 1),
                parse_failure=recipe.get("parse_failure"))
            text, kind, completeness = fetched.text, fetched.text_kind, fetched.completeness
            published = fetched.published or hit.get("published") or source_time(None)
            if published.get("value") is None and hit.get("published", {}).get("value") is not None:
                published = hit["published"]
            if source["kind"] == "sec_submissions":
                published = hit.get("published", source_time(None))
        else:
            fetched = None
            text, kind, completeness = hit.get("text") or hit.get("snippet", ""), hit.get("text_kind", "snippet"), hit.get("completeness", "unknown")
            published = hit.get("published", source_time(None))
    except (KeyError, IndexError, TypeError, ValueError):
        raise IntelligenceError("HASH_MISMATCH", "Document normalization cannot be replayed") from None
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    if recipe.get("raw_redacted"):
        completeness = "partial"
    if (document["text"] != text or document["text_blob"]["sha256"] != content_hash
            or document["text_kind"] != kind or document["completeness"] != completeness
            or document["times"]["published"] != published):
        raise IntelligenceError("HASH_MISMATCH", "Replayed source text/time differs from document")
    expected_title = (fetched.title if fetched else hit.get("title")) or hit.get("title", "")
    expected_updated = (fetched.updated if fetched else hit.get("updated")) or source_time(None)
    if document["title"] != expected_title or document["times"]["updated"] != expected_updated or document["original_url"] != hit["url"]:
        raise IntelligenceError("HASH_MISMATCH", "Replayed source title/update/original URL differs")
    authority = hit.get("raw_metadata", {}).get("authority", {})
    if source["kind"] in {"search", "web_fetch"} or authority.get("scope") not in source["authority_scopes"]:
        authority = {"scope": "discovery", "series_ids": [], "proof_type": "unverified", "proof_ref": {}}
    expected_authority = json.loads(canonical(authority))
    proof = expected_authority.setdefault("proof_ref", {})
    proof.update(source_id=document["source_id"], content_sha256=content_hash)
    proof.setdefault("source_url", discovery_recipe["response_url"])
    proof.setdefault("response_sha256", discovery_recipe.get("transport_sha256", discovery_recipe["raw_response_ref"]["sha256"]))
    proof["discovery_response_ref"] = discovery_recipe["raw_response_ref"]
    if fetched:
        proof["source_url"] = fetched.final_url
        proof["response_sha256"] = recipe.get("transport_sha256", recipe["raw_response_ref"]["sha256"])
        proof["original_response_ref"] = recipe["raw_response_ref"]
    if expected_authority != document["authority"]:
        raise IntelligenceError("HASH_MISMATCH", "Authority/time proof cannot be reconstructed from raw source responses")
    expected_url = fetched.final_url if fetched else hit["url"]
    if (document["final_url"] != expected_url or document["canonical_url"] != canonical_url(expected_url)
            or document["raw_blob"] != recipe["raw_response_ref"] or document["raw_ref"] != document["raw_blob"]):
        raise IntelligenceError("HASH_MISMATCH", "Document URL/raw reference differs from parser replay")
    expected_metadata = hit.get("raw_metadata", {})
    if (document.get("publisher_id") != urlsplit(expected_url).hostname
            or document.get("origin_cluster") != urlsplit(expected_url).hostname
            or document.get("source_metadata") != expected_metadata
            or document.get("attachments", []) != (fetched.attachments if fetched else hit.get("attachments", []))
            or document["times"]["event"] != (expected_metadata.get("event_time") or source_time(None))
            or document["times"]["retrieved"] != source_time(recipe["received_at"], evidence={"kind": "source_transport"})):
        raise IntelligenceError("HASH_MISMATCH", "Document origin/event/source metadata differs from raw parser replay")
    version_input = {"canonical_url": document["canonical_url"], "source_id": document["source_id"],
        "text_sha256": content_hash, "text_kind": kind, "published": published,
        "updated": (fetched.updated if fetched else hit.get("updated")), "completeness": completeness,
        **({"extractor_config_sha256": (fetched.metadata if fetched else hit.get("metadata", {}))["extractor_config_sha256"]}
           if (fetched.metadata if fetched else hit.get("metadata", {})).get("extractor_config_sha256") else {}),
        **({"source_contract_sha256": contract_hash} if contract_hash else {})}
    expected_version = digest(version_input)
    # Before redacted-html-links/v1, default builtin HTML extraction did not
    # include a parser configuration hash. The new placeholder-link omission
    # can add that metadata even when the exact archived text, time, URL and
    # attachments above still reconstruct identically. Recognize only this
    # historical shape; explicit policies and recorded hashes remain strict.
    # No failed/partial extraction is upgraded and no source byte is changed.
    recorded_metadata = document.get("extraction_metadata", {})
    parameters = recipe.get("parameters", {})
    observed_metadata = fetched.metadata if fetched else hit.get("metadata", {})
    if (not isinstance(recorded_metadata, dict)
            or "extractor_config_sha256" in recorded_metadata
            and recorded_metadata["extractor_config_sha256"] != observed_metadata.get("extractor_config_sha256")
            or expected_version == document["version"] and observed_metadata.get("extractor_config_sha256")
            and "extractor_config_sha256" not in recorded_metadata):
        raise IntelligenceError("HASH_MISMATCH", "Recorded parser configuration differs from raw replay")
    legacy_default_redaction = (
        source.get("kind") == "web_fetch" and not source.get("options", {}).get("extractor")
        and recipe.get("operation") == "fetch" and recipe.get("raw_redacted") is True
        and "_parse_policy" not in parameters
        and "_parse_policy" not in parameters.get("context", {})
        and isinstance(recorded_metadata, dict)
        and recorded_metadata.get("extractor") == "tiger-main-text/v1"
        and "extractor_config_sha256" not in recorded_metadata
        and "redaction_markup_policy" not in recorded_metadata
        and observed_metadata.get("extractor") == "tiger-main-text/v1"
        and observed_metadata.get("redaction_markup_policy") == "tiger.redacted-html-links/v1"
        and kind in {"html_main", "html_text"} and completeness == "partial")
    version_contract = "recorded_normalization"
    if expected_version != document["version"] and legacy_default_redaction:
        expected_version = digest({key: value for key, value in version_input.items()
                                   if key != "extractor_config_sha256"})
        version_contract = "legacy-default-redacted-html-without-parser-hash/v1"
    expected_doc_id = "intel_" + hashlib.sha256(document["canonical_url"].encode()).hexdigest()[:24]
    if (document["version"] != expected_version or document["doc_id"] != expected_doc_id
            or document["id"] != "docver_" + digest({"doc_id": expected_doc_id, "version": expected_version})[:32]):
        raise IntelligenceError("HASH_MISMATCH", "Document content-version identity differs")
    if document["authority"].get("scope") == "sec_filing" and document["authority"].get("proof_type") in {"sec_submission_acceptance", "immutable_accession"}:
        proof = document["authority"]["proof_ref"]
        original = authority.get("proof_ref", {})
        directory = original.get("issuer_directory_ref")
        if (not directory or proof.get("accession") != original.get("accession")
                or proof.get("accepted_at") != original.get("accepted_at")
                or document["authority"].get("issuer_id") != authority.get("issuer_id")):
            raise IntelligenceError("HASH_MISMATCH", "SEC submission acceptance chain differs")
        raw_directory = json.loads(read_blob(directory["raw_response_ref"]))
        values = list(raw_directory.values())
        if "fields" in raw_directory and "data" in raw_directory:
            values = [dict(zip(raw_directory["fields"], row)) for row in raw_directory["data"]]
        match = [row for row in values if isinstance(row, dict) and row.get("ticker", "").upper() == directory["ticker"]]
        if len(match) != 1 or str(match[0].get("cik_str", match[0].get("cik"))).zfill(10) != directory["cik"]:
            raise IntelligenceError("HASH_MISMATCH", "SEC ticker-to-CIK directory chain differs")
        parent = original.get("submissions_parent_ref")
        if parent:
            raw_parent = json.loads(read_blob(parent["raw_response_ref"]))
            if (str(raw_parent.get("cik", "")).zfill(10) != proof["cik"]
                    or raw_parent.get("name", "") != parent["issuer_name"]
                    or not any(row.get("name") == original["submissions_file"]
                        for row in raw_parent.get("filings", {}).get("files", []) if isinstance(row, dict))):
                raise IntelligenceError("HASH_MISMATCH", "SEC historical submissions file is not linked by issuer index")
    if document["authority"]["proof_ref"].get("content_sha256") != content_hash:
        raise IntelligenceError("HASH_MISMATCH", "Authority proof is not bound to normalized text")
    return {"text_sha256": content_hash, "text_kind": kind, "completeness": completeness,
            "published": published, "source_id": document["source_id"],
            "raw_response_sha256": recipe["raw_response_ref"]["sha256"],
            **({"version_contract": version_contract}
               if version_contract != "recorded_normalization" else {})}


def verify_bundle(root, bundle_id, *, tenant="public", expected_plan_hash=None, expected_registry_sha256=None):
    archive = Archive(root, tenant=tenant, readonly=True)
    bundle = archive.bundle(bundle_id)
    SourceRegistry.verify_manifest(bundle["source_registry"])
    if expected_plan_hash is not None and bundle["plan_hash"] != expected_plan_hash:
        raise IntelligenceError("PLAN_CONFLICT", "Bundle plan differs from trusted execution")
    if expected_registry_sha256 is not None and bundle["source_registry"]["sha256"] != expected_registry_sha256:
        raise IntelligenceError("PLAN_CONFLICT", "Bundle source policy differs from trusted execution")
    for document in bundle["documents"]:
        reconstruct_document(document, archive.read_blob, bundle["source_registry"])
    return bundle
