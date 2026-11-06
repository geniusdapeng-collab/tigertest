"""Discovery/fetch adapters; provider summaries never become generated analysis."""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from urllib.parse import urlsplit

from ..redline import LLMUnavailable
from .contracts import (FetchResult, SearchRequest, SearchResult, SourceError,
                        canonical, credential_free_url, safe_value, source_time)
from .extract import extract_response
from .transport import HttpTransport


def _hit(row, *, url, title, snippet, text="", published=None, text_kind="snippet", metadata=None):
    if not isinstance(url, str) or not credential_free_url(url) or not isinstance(title, str):
        raise SourceError("CONTRACT_INVALID", delivery="returned")
    return {"url": url, "title": title, "snippet": snippet if isinstance(snippet, str) else "",
            "text": text if isinstance(text, str) else "", "text_kind": text_kind,
            "completeness": "unknown", "published": source_time(published),
            "raw_metadata": safe_value(metadata if metadata is not None else row)}


def _rows(value, response_bytes=0):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=response_bytes)
    return value


class WebFetch:
    def __init__(self, transport: HttpTransport | None = None, *, parser_policy=None):
        self.transport = transport or HttpTransport()
        self.parse_policy = parser_policy

    def fetch(self, url: str) -> FetchResult:
        response = self.transport.request("GET", url, headers={"User-Agent": "WorkLoom-Tiger-Research/1.0"})
        return extract_response(response, original_url=url, policy=self.parse_policy,
                                deadline=getattr(self.transport, "deadline", None))


class _SearchAdapter(WebFetch):
    source_id = ""
    endpoint = ""

    def __init__(self, transport: HttpTransport | None = None, credential: str | None = None,
                 *, endpoint: str | None = None):
        super().__init__(transport)
        self.credential = credential
        if endpoint is not None:
            self.endpoint = endpoint

    def available(self) -> dict:
        return {"available": bool(self.credential), "reason": None if self.credential else "SOURCE_AUTH_REQUIRED"}

    def _require(self):
        if not self.credential:
            raise SourceError("SOURCE_AUTH_REQUIRED", delivery="not_sent")

    def _response(self, response, request, hits, payload, warnings=None):
        return SearchResult(hits, request,
            {"transport_sha256": response.transport_sha256, "received_at": response.received_at,
             "response_url": response.url, "http_headers": response.headers,
             "request_count": response.request_count,
             "response": safe_value(payload, (self.credential or "",))},
            self._usage(payload), warnings or [], response.body)

    def _usage(self, payload):
        value = payload.get("usage") if isinstance(payload, dict) else None
        return {"state": "reported", "provider_usage": safe_value(value), "requires_reconciliation": False} if isinstance(value, dict) else {
            "state": "unknown", "requires_reconciliation": True}


class BraveAdapter(_SearchAdapter):
    source_id = "brave"
    endpoint = "https://api.search.brave.com/res/v1/news/search"

    def search(self, request: SearchRequest) -> SearchResult:
        self._require()
        if len(request.query) > 400 or len(request.query.split()) > 50 or request.count > 50:
            raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "BRAVE_QUERY_LIMIT"})
        q = request.query + (" " + " ".join("site:" + x for x in request.domains) if len(request.domains) == 1 else "")
        params = {"q": q, "count": request.count, "offset": request.page,
                  "search_lang": request.language, "country": "US", "extra_snippets": "true"}
        warnings = []
        if request.start_date or request.end_date:
            params["freshness"] = (request.start_date or "1900-01-01") + "to" + (request.end_date or "2100-01-01")
        if len(request.domains) > 1:
            raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "SPLIT_DOMAINS_REQUIRED"})
        response = self.transport.request("GET", self.endpoint, params=params,
            headers={"X-Subscription-Token": self.credential, "Accept": "application/json"})
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        if rows is None and isinstance(payload, dict):
            rows = payload.get("news", {}).get("results")
        rows = _rows(rows, len(response.body))
        hits = [_hit(row, url=row.get("url"), title=row.get("title", ""), snippet=row.get("description", ""),
                     published=row.get("page_age") or row.get("published_time"), metadata=row) for row in rows]
        warnings.append("Provider freshness can refer to publication or modification; original version time still requires verification")
        return self._response(response, params, hits, payload, warnings)


class TavilyAdapter(_SearchAdapter):
    source_id = "tavily"
    endpoint = "https://api.tavily.com/search"

    def search(self, request: SearchRequest) -> SearchResult:
        self._require()
        if request.page or request.count > 20:
            raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "TAVILY_UNSUPPORTED_PAGE_OR_COUNT"})
        body = {"query": request.query, "search_depth": "advanced", "max_results": request.count,
                "topic": "news" if request.topic == "news" else "general",
                "include_answer": False, "include_raw_content": "markdown", "auto_parameters": False,
                "include_images": False, "include_usage": True, "language": request.language,
                "include_published_date": True, "filter_by_published_date": False}
        if request.domains:
            body["include_domains"] = list(request.domains)
        if request.start_date:
            body["start_date"] = request.start_date
        if request.end_date:
            body["end_date"] = request.end_date
        response = self.transport.request("POST", self.endpoint, json_body=body,
            headers={"Authorization": "Bearer " + self.credential, "Content-Type": "application/json"})
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        rows = _rows(rows, len(response.body))
        hits = [_hit(row, url=row.get("url"), title=row.get("title", ""), snippet=row.get("content", ""),
                     text=row.get("raw_content") or "", text_kind="provider_markdown" if row.get("raw_content") else "snippet",
                     published=row.get("published_date"), metadata=row) for row in rows]
        return self._response(response, body, hits, payload,
            ["Provider publication-date estimates and cache age are separate from strict original-version availability"])


class ExaAdapter(_SearchAdapter):
    source_id = "exa"
    endpoint = "https://api.exa.ai/search"

    def _usage(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("costDollars"), dict):
            return {"state": "reported", "provider_usage": {"costDollars": safe_value(payload["costDollars"])},
                    "requires_reconciliation": False}
        return super()._usage(payload)

    def search(self, request: SearchRequest) -> SearchResult:
        self._require()
        if request.page or request.count > 100:
            raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "EXA_UNSUPPORTED_PAGINATION"})
        body = {"query": request.query, "type": "auto", "numResults": request.count,
                "contents": {"text": True}}
        if request.topic == "news":
            body["category"] = "news"
        if request.domains:
            body["includeDomains"] = list(request.domains)
        if request.start_date:
            body["startPublishedDate"] = request.start_date + "T00:00:00.000Z"
        if request.end_date:
            body["endPublishedDate"] = request.end_date + "T23:59:59.999Z"
        response = self.transport.request("POST", self.endpoint, json_body=body,
            headers={"x-api-key": self.credential, "Content-Type": "application/json"})
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        rows = _rows(rows, len(response.body))
        if any(row.get("highlights") is not None and (not isinstance(row["highlights"], list)
                or any(not isinstance(value, str) for value in row["highlights"])) for row in rows):
            raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(response.body))
        hits = [_hit(row, url=row.get("url"), title=row.get("title", ""), snippet="\n".join(row.get("highlights") or []),
                     text=row.get("text") or "", text_kind="provider_text" if row.get("text") else "snippet",
                     published=row.get("publishedDate"), metadata=row) for row in rows]
        warnings = payload.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        return self._response(response, body, hits, payload, warnings)


def deepseek_query(request: SearchRequest) -> str:
    if len(request.domains) > 1:
        raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "SPLIT_DOMAINS_REQUIRED"})
    query = request.query
    if request.domains:
        query += " site:" + request.domains[0]
    if request.language:
        query += " lang:" + request.language
    if request.start_date:
        query += " after:" + (date.fromisoformat(request.start_date) - timedelta(days=1)).isoformat()
    if request.end_date:
        query += " before:" + (date.fromisoformat(request.end_date) + timedelta(days=1)).isoformat()
    if len(query) > 128 or request.count > 50 or request.page:
        raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "DEEPSEEK_QUERY_LIMIT"})
    return query


class DeepSeekAdapter(_SearchAdapter):
    """Native raw Search/Fetch for separately authorized acquisition/benchmarks."""
    source_id = "deepseek"
    endpoint = "https://api.deepseek.com/v1/tools/search"

    def _usage(self, payload):
        # Native Search/Fetch are billed per successful request, not generated
        # tokens. A completed response proves the known request unit even when
        # the endpoint does not include a separate usage object.
        return {"state": "reported_requests", "successful_requests": 1,
                "requires_reconciliation": False}

    def search(self, request: SearchRequest) -> SearchResult:
        self._require()
        body = {"query": deepseek_query(request), "count": request.count, "need_content": True}
        response = self.transport.request("POST", self.endpoint, json_body=body,
            headers={"Authorization": "Bearer " + self.credential, "Content-Type": "application/json"})
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        rows = _rows(rows, len(response.body))
        hits = [_hit(row, url=row.get("url"), title=row.get("title", ""),
                     snippet=row.get("summary") or row.get("snippet") or "",
                     text=row.get("content") or "", text_kind="provider_markdown" if row.get("content") else "snippet",
                     published=row.get("date"), metadata={k: v for k, v in row.items() if k != "content"}) for row in rows]
        return self._response(response, body, hits, payload,
            ["Native content is provider Markdown; original page/version availability requires verification"])

    def fetch(self, url: str) -> FetchResult:
        self._require()
        endpoint = self.endpoint.rsplit("/", 1)[0] + "/fetch"
        response = self.transport.request("POST", endpoint, json_body={"url": url},
            headers={"Authorization": "Bearer " + self.credential, "Content-Type": "application/json"})
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        return FetchResult(url, payload.get("url", url), payload.get("title", ""), payload["content"],
            canonical(safe_value(payload, (self.credential,))), "application/json", "provider_markdown", "unknown",
            source_time(payload.get("date")), metadata={"transport_sha256": response.transport_sha256,
                "retrieved_at": response.received_at, "raw_format": "sanitized_provider_response",
                "response": safe_value(payload, (self.credential,))}, usage=self._usage(payload))


class DeepSeekBridge:
    """Use the admitted existing client; no alternate model/key or hidden transport."""
    source_id = "deepseek"

    def __init__(self, client):
        self.client = client

    def available(self):
        return {"available": self.client is not None, "reason": None if self.client is not None else "SOURCE_AUTH_REQUIRED"}

    @staticmethod
    def _failure(exc):
        diagnostic = getattr(exc, "diagnostic", {})
        diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
        status = diagnostic.get("http_status")
        native_code = diagnostic.get("error_type") or diagnostic.get("code")
        code = native_code if native_code in {"BUDGET_EXHAUSTED", "DEADLINE_EXCEEDED", "RESPONSE_TOO_LARGE"} else (
            "RATE_LIMITED" if status == 429 else "SOURCE_UNAVAILABLE")
        count = diagnostic.get("request_count")
        received = diagnostic.get("bytes_received", 0)
        return SourceError(code, http_status=status,
            delivery="not_sent" if count == 0 else "returned" if status is not None else "unknown",
            bytes_received=received if type(received) is int and received >= 0 else 0,
            details={"request_count": count} if type(count) is int and count >= 0 else {})

    def search(self, request: SearchRequest) -> SearchResult:
        if self.client is None:
            raise SourceError("SOURCE_AUTH_REQUIRED", delivery="not_sent")
        query = deepseek_query(request)
        try:
            rows = self.client.search(query, count=request.count, need_content=True)
        except LLMUnavailable as exc:
            raise self._failure(exc) from None
        rows = _rows(rows)
        hits = [_hit(row, url=row.get("url"), title=row.get("title", ""), snippet=row.get("summary", ""),
                     text=row.get("content", ""), text_kind="provider_text", published=row.get("published"),
                     metadata={k: v for k, v in row.items() if k != "content"}) for row in rows]
        receipt = getattr(self.client, "search_receipts", [])
        return SearchResult(hits, {"query": query, "count": request.count, "need_content": True},
            {"bridge": "admitted-deepseek-client", "receipt": receipt[-1] if receipt else None},
            {"state": "reported_requests", "successful_requests": 1, "requires_reconciliation": False},
            ["Compatibility bridge cannot distinguish provider content from summary/snippet; Fetch is required for original-page evidence"])

    def fetch(self, url: str) -> FetchResult:
        if self.client is None:
            raise SourceError("SOURCE_AUTH_REQUIRED", delivery="not_sent")
        try:
            row = self.client.fetch(url)
        except LLMUnavailable as exc:
            raise self._failure(exc) from None
        if not isinstance(row, dict) or not isinstance(row.get("content"), str):
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        text = row["content"]
        receipt = getattr(self.client, "fetch_receipts", [])
        return FetchResult(url, row.get("url", url), row.get("title", ""), text, canonical(row),
            "application/json", "provider_markdown", "unknown",
            source_time(row.get("published")), metadata={"bridge": "admitted-deepseek-client",
                "receipt": receipt[-1] if receipt else None, "raw_format": "sanitized_provider_projection",
                "transport_sha256": digest_bytes(canonical(row))},
            usage={"state": "reported_requests", "successful_requests": 1, "requires_reconciliation": False})


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
