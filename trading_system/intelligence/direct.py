"""Registered first-party feeds, disclosures, calendars and versioned data."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from io import StringIO
import math
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from .adapters import WebFetch
from .contracts import SearchRequest, SearchResult, SourceError, safe_value, source_time
from .extract import extract_response


def _xml(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise SourceError("CONTRACT_INVALID", delivery="returned", details={"reason": "XML_EXTERNAL_ENTITY_DENIED"})
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise SourceError("CONTRACT_INVALID", delivery="returned") from None


def _local(element):
    return element.tag.rsplit("}", 1)[-1]


def _child(element, name):
    return next((child for child in element if _local(child) == name), None)


def _text(element, name):
    child = _child(element, name)
    return "" if child is None else "".join(child.itertext()).strip()


def _metadata(observed, **extra):
    return {"transport_sha256": observed.transport_sha256, "received_at": observed.received_at,
            "response_bytes": len(observed.body), "response_url": observed.url,
            "http_headers": observed.headers, "request_count": observed.request_count, **extra}


def _conditional(request):
    value = request.context.get("conditional", {})
    if not isinstance(value, dict) or set(value) - {"etag", "last_modified"}:
        raise SourceError("CONTRACT_INVALID", delivery="not_sent")
    headers = {}
    for key, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
        if value.get(key) is not None:
            text = value[key]
            if not isinstance(text, str) or not text or len(text) > 2048 or any(ord(x) < 32 for x in text):
                raise SourceError("CONTRACT_INVALID", delivery="not_sent")
            headers[header] = text
    return headers


def _not_modified(response, request):
    return SearchResult([], {"url": response.url, "conditional": request.context.get("conditional", {})},
        _metadata(response, http_status=304, not_modified=True, complete_history=False),
        {"state": "public_unmetered", "requests": response.request_count,
         "bytes": 0, "requires_reconciliation": False}, ["NOT_MODIFIED_CHECK_ONLY"], b"")


def _windowed(hits, request):
    matching, unknown = [], 0
    for hit in hits:
        value = hit.get("published", {}).get("value")
        if value:
            day = value[:10]
            if request.start_date and day < request.start_date or request.end_date and day > request.end_date:
                continue
        else:
            unknown += 1
        matching.append(hit)
    start = request.page * request.count
    return matching[start:start + request.count], {"total_items": len(hits), "matching_items": len(matching),
        "returned_items": len(matching[start:start + request.count]), "has_more": start + request.count < len(matching),
        "unknown_publication_items_retained": unknown, "time_filter": "known_publication_dates_only"}


class FeedSource(WebFetch):
    def __init__(self, spec, transport, *, user_agent=None, credential=None):
        super().__init__(transport)
        self.spec, self.user_agent, self.credential = spec, user_agent, credential

    def _headers(self, url):
        headers = {"User-Agent": self.user_agent or "WorkLoom-Tiger-Research/1.0"}
        def origin(value):
            parsed = urlsplit(value)
            return parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        # Content hosts are an allowlist for fetching public attachments, not an
        # authorization grant to receive the registered endpoint's credential.
        if self.credential and origin(url) == origin(self.spec.endpoint):
            headers["Authorization"] = "Bearer " + self.credential
        return headers

    def available(self):
        if self.spec.credential_env and not self.credential:
            return {"available": False, "reason": "SOURCE_AUTH_REQUIRED"}
        configured = bool(self.spec.endpoint) and (self.spec.kind != "issuer_rss" or bool(self.spec.options.get("issuer_id")))
        if any(host in {"sec.gov", "www.sec.gov", "data.sec.gov"} for host in self.spec.allowed_hosts) and not self.user_agent:
            configured = False
        return {"available": configured, "reason": None if configured else "SOURCE_IDENTITY_REQUIRED"}

    def search(self, request: SearchRequest):
        if not self.available()["available"]:
            raise SourceError(self.available()["reason"], delivery="not_sent")
        headers = self._headers(self.spec.endpoint)
        conditional = _conditional(request)
        headers.update(conditional)
        response = self.transport.request("GET", self.spec.endpoint, headers=headers,
            allowed_hosts=self.spec.allowed_hosts, allow_not_modified=bool(conditional))
        if response.status == 304:
            return _not_modified(response, request)
        root = _xml(response.body)
        items = [e for e in root.iter() if _local(e) in {"item", "entry"}]
        hits = []
        for item in items:
            links = [c for c in item if _local(c) == "link"]
            url = next((c.attrib.get("href") for c in links if c.attrib.get("rel", "alternate") == "alternate"), None)
            url = url or _text(item, "link") or _text(item, "guid")
            if not url.startswith(("https://", "http://")):
                continue
            title = _text(item, "title")
            description = _text(item, "description") or _text(item, "summary") or _text(item, "content")
            raw_date = _text(item, "published") or _text(item, "pubDate") or _text(item, "updated")
            published = source_time(raw_date, evidence={"kind": "official_feed", "url": self.spec.endpoint,
                                                       "response_sha256": response.transport_sha256})
            authority = {"scope": self.spec.authority_scopes[0] if self.spec.authority_scopes else "discovery",
                "issuer_id": self.spec.options.get("issuer_id"), "series_ids": [],
                "proof_type": "official_release", "proof_ref": {"source_id": self.spec.source_id,
                    "source_url": self.spec.endpoint, "response_sha256": response.transport_sha256}}
            hits.append({"url": url, "title": title, "snippet": description,
                "text": "", "text_kind": "feed", "completeness": "partial", "published": published,
                "raw_metadata": {"feed_id": _text(item, "id") or _text(item, "guid"),
                    "feed_url": self.spec.endpoint, "authority": authority,
                    "categories": [_text(c, "term") or c.attrib.get("term", "") for c in item if _local(c) == "category"]}})
        # Feed items remain discovery records; a full feed does not prove that
        # older items/filings never existed. Cursor/overlap is explicit metadata.
        selected, window = _windowed(hits, request)
        return SearchResult(selected, {"url": self.spec.endpoint, "count": request.count, "page": request.page,
            "start_date": request.start_date, "end_date": request.end_date},
            _metadata(response, feed_items=len(hits), complete_history=False, **window),
            {"state": "public_unmetered", "requests": 1, "bytes": len(response.body),
             "requires_reconciliation": False}, ["Feed is a bounded discovery window, not complete filing history",
                "Date filtering retains unknown precision entries and cannot prove an entire historical window is covered"], response.body)

    def fetch(self, url):
        if not self.available()["available"]:
            raise SourceError(self.available()["reason"], delivery="not_sent")
        response = self.transport.request("GET", url,
            headers=self._headers(url),
            allowed_hosts=self.spec.allowed_hosts)
        filing = (self.spec.kind == "sec_submissions" and re.fullmatch(
            r"/Archives/edgar/data/\d{1,10}/\d{18}/[^/]+", urlsplit(url).path) is not None)
        return extract_response(response, original_url=url, trusted_filing=filing,
            policy=self.parse_policy, deadline=getattr(self.transport, "deadline", None))


class RegisteredPageSource(FeedSource):
    """A trusted fixed original page/PDF/table, not a user-supplied URL tool."""
    def __init__(self, spec, transport, *, credential=None, user_agent=None):
        super().__init__(spec, transport, user_agent=user_agent, credential=credential)

    def available(self):
        if self.spec.credential_env and not self.credential:
            return {"available": False, "reason": "SOURCE_AUTH_REQUIRED"}
        return super().available()

    def fetch(self, url):
        if not self.available()["available"]:
            raise SourceError(self.available()["reason"], delivery="not_sent")
        headers = self._headers(url)
        response = self.transport.request("GET", url, headers=headers, allowed_hosts=self.spec.allowed_hosts)
        return extract_response(response, original_url=url, policy=self.parse_policy,
                                deadline=getattr(self.transport, "deadline", None))

    def search(self, request):
        if not self.available()["available"]:
            raise SourceError(self.available()["reason"], delivery="not_sent")
        conditional = _conditional(request)
        headers = {**self._headers(self.spec.endpoint), **conditional}
        response = self.transport.request("GET", self.spec.endpoint, headers=headers,
            allowed_hosts=self.spec.allowed_hosts, allow_not_modified=bool(conditional))
        if response.status == 304:
            return _not_modified(response, request)
        fetched = extract_response(response, original_url=self.spec.endpoint, policy=self.parse_policy,
                                   deadline=getattr(self.transport, "deadline", None))
        authority = {"scope": self.spec.authority_scopes[0], "issuer_id": self.spec.options.get("issuer_id"),
            "series_ids": self.spec.options.get("series_ids", []), "proof_type": "unverified",
            "proof_ref": {"source_id": self.spec.source_id, "source_url": response.url,
                          "response_sha256": response.transport_sha256}}
        hit = {"url": fetched.final_url, "title": fetched.title or self.spec.options.get("title", self.spec.source_id),
            "snippet": "", "text": fetched.text, "text_kind": fetched.text_kind,
            "completeness": fetched.completeness, "published": fetched.published, "updated": fetched.updated,
            "metadata": fetched.metadata, "attachments": fetched.attachments,
            "raw_metadata": {"authority": authority, "original_material": True,
                "material_types": self.spec.options.get("material_types", []),
                "publisher": self.spec.options.get("publisher"), "business_unit": self.spec.options.get("business_unit"),
                "author_ids": self.spec.options.get("author_ids", []),
                "entitlement_ref": self.spec.options.get("entitlement_ref")}}
        return SearchResult([hit], {"url": self.spec.endpoint, "conditional": request.context.get("conditional", {})},
            _metadata(response, http_status=response.status, not_modified=False),
            {"state": "public_unmetered", "requests": response.request_count, "bytes": len(response.body),
             "requires_reconciliation": False}, [], response.body)


class SecSubmissionsSource(FeedSource):
    def available(self):
        return {"available": bool(self.user_agent), "reason": None if self.user_agent else "SOURCE_IDENTITY_REQUIRED"}

    def search(self, request: SearchRequest):
        if not self.available()["available"]:
            raise SourceError("SOURCE_IDENTITY_REQUIRED", delivery="not_sent")
        cik = request.context.get("cik") or self.spec.options.get("cik")
        if not isinstance(cik, (str, int)) or not str(cik).isdigit() or not 1 <= len(str(cik)) <= 10:
            raise SourceError("SOURCE_IDENTITY_REQUIRED", delivery="not_sent", details={"reason": "CIK_REQUIRED"})
        cik = str(cik).lstrip("0") or "0"
        historical = request.context.get("submissions_file")
        if historical is not None and (not isinstance(historical, dict) or re.fullmatch(
                r"CIK" + cik.zfill(10) + r"-submissions-\d{3,}\.json", str(historical.get("name", ""))) is None
                or not isinstance(historical.get("parent_ref"), dict)):
            raise SourceError("CONTRACT_INVALID", delivery="not_sent", details={"reason": "SUBMISSIONS_FILE_IDENTITY_INVALID"})
        url = self.spec.endpoint.rstrip("/") + "/" + (historical["name"] if historical else "CIK" + cik.zfill(10) + ".json")
        response = self.transport.request("GET", url, headers={"User-Agent": self.user_agent,
            "Accept": "application/json"}, allowed_hosts=self.spec.allowed_hosts)
        payload = response.json()
        if not isinstance(payload, dict) or not historical and str(payload.get("cik", "")).lstrip("0") != cik:
            raise SourceError("CONTRACT_INVALID", delivery="returned", details={"reason": "CIK_MISMATCH"})
        filings = payload.get("filings", {})
        if not historical and (not isinstance(filings, dict) or not isinstance(filings.get("files", []), list)):
            raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(response.body))
        if not historical:
            for older in filings.get("files", []):
                if (not isinstance(older, dict) or not isinstance(older.get("name"), str)
                        or any(source_time(older.get(key))["precision"] != "date" for key in ("filingFrom", "filingTo"))
                        or older["filingFrom"] > older["filingTo"]):
                    raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(response.body),
                                      details={"reason": "SUBMISSIONS_FILE_CONTRACT_INVALID"})
        recent = payload if historical else filings.get("recent")
        if not isinstance(recent, dict) or not isinstance(recent.get("accessionNumber"), list):
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        size = len(recent["accessionNumber"])
        if size and any(field not in recent for field in ("form", "primaryDocument", "filingDate")):
            raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(response.body),
                              details={"reason": "FILING_COLUMN_REQUIRED"})
        for field in ("form", "primaryDocument", "filingDate", "acceptanceDateTime", "reportDate", "primaryDocDescription"):
            if field in recent and (not isinstance(recent[field], list) or len(recent[field]) != size
                    or any(not isinstance(value, str) for value in recent[field])):
                raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(response.body),
                                  details={"reason": "FILING_COLUMN_CONTRACT_INVALID"})
        hits = []
        forms = set(self.spec.options.get("forms", ["10-K", "10-Q", "8-K", "6-K", "20-F", "10-K/A", "10-Q/A", "8-K/A"]))
        def at(name, index):
            values = recent.get(name, [])
            return values[index] if isinstance(values, list) and index < len(values) else ""
        for index, accession in enumerate(recent["accessionNumber"]):
            form, document = at("form", index), at("primaryDocument", index)
            if form not in forms:
                continue
            if re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(accession)) is None or re.fullmatch(r"[A-Za-z0-9_.\-]+", str(document)) is None:
                raise SourceError("CONTRACT_INVALID", delivery="returned", details={"reason": "FILING_IDENTITY_INVALID"})
            filed = at("filingDate", index)
            try:
                datetime.strptime(filed, "%Y-%m-%d")
            except ValueError:
                raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(response.body),
                                  details={"reason": "FILING_DATE_INVALID"}) from None
            if request.start_date and filed < request.start_date or request.end_date and filed > request.end_date:
                continue
            accepted = at("acceptanceDateTime", index)
            published = source_time(accepted or filed, evidence={"kind": "sec_submissions", "url": url,
                                        "response_sha256": response.transport_sha256, "accession": accession})
            filing_url = "https://www.sec.gov/Archives/edgar/data/" + cik + "/" + accession.replace("-", "") + "/" + document
            issuer = "issuer:" + cik.zfill(10)
            directory_ref = request.context.get("issuer_directory_ref")
            authority = {"scope": "sec_filing", "issuer_id": issuer, "series_ids": [],
                "proof_type": "sec_submission_acceptance" if published["precision"] == "instant" and directory_ref else "unverified",
                "proof_ref": {"source_id": self.spec.source_id, "source_url": url,
                    "response_sha256": response.transport_sha256, "accession": accession,
                    "accepted_at": published["value"] if published["precision"] == "instant" else None,
                    "cik": cik.zfill(10), "primary_document": document,
                    "issuer_directory_ref": directory_ref,
                    **({"submissions_parent_ref": historical["parent_ref"], "submissions_file": historical["name"]} if historical else {})}}
            issuer_name = request.context.get("issuer_name", "") if historical else payload.get("name", "")
            hits.append({"url": filing_url, "title": str(issuer_name) + " " + form + " " + filed,
                "snippet": at("primaryDocDescription", index), "text": "", "text_kind": "filing_index",
                "completeness": "partial", "published": published,
                "raw_metadata": {"authority": authority, "cik": cik, "accession": accession,
                    "form": form, "report_date": at("reportDate", index),
                    "filing_date": filed, "is_amendment": form.endswith("/A")}})
        selected, window = _windowed(hits, request)
        return SearchResult(selected, {"url": url, "cik": cik, "forms": sorted(forms),
            "count": request.count, "page": request.page,
            "start_date": request.start_date, "end_date": request.end_date},
            _metadata(response, response=payload, older_files=[] if historical else payload.get("filings", {}).get("files", []), **window),
            {"state": "public_unmetered", "requests": 1, "bytes": len(response.body), "requires_reconciliation": False}, raw_bytes=response.body)


class SecDirectorySource(FeedSource):
    def available(self):
        return {"available": bool(self.user_agent), "reason": None if self.user_agent else "SOURCE_IDENTITY_REQUIRED"}

    def search(self, request: SearchRequest):
        if not self.user_agent:
            raise SourceError("SOURCE_IDENTITY_REQUIRED", delivery="not_sent")
        response = self.transport.request("GET", self.spec.endpoint,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            allowed_hosts=self.spec.allowed_hosts)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        rows = list(payload.values())
        if "fields" in payload and "data" in payload:
            rows = [dict(zip(payload["fields"], row)) for row in payload["data"]]
        mapping = {}
        for row in rows:
            if not isinstance(row, dict):
                raise SourceError("CONTRACT_INVALID", delivery="returned")
            ticker, cik = row.get("ticker"), row.get("cik_str", row.get("cik"))
            if (not isinstance(ticker, str) or not isinstance(cik, (str, int))
                    or not str(cik).isdigit() or len(str(cik)) > 10):
                raise SourceError("CONTRACT_INVALID", delivery="returned")
            normalized = ticker.upper()
            if normalized in mapping and mapping[normalized]["cik"] != str(cik).zfill(10):
                raise SourceError("EVIDENCE_CONFLICT", delivery="returned")
            mapping[normalized] = {"cik": str(cik).zfill(10), "name": row.get("title", row.get("name", "")),
                                   "exchange": row.get("exchange")}
        text = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        hit = {"url": self.spec.endpoint, "title": "SEC issuer/ticker directory", "snippet": "",
            "text": text, "text_kind": "identity_directory", "completeness": "complete",
            "published": source_time(None), "raw_metadata": {"identity_directory": mapping,
                "authority": {"scope": "discovery", "proof_type": "unverified", "series_ids": [],
                    "proof_ref": {"source_id": self.spec.source_id, "source_url": self.spec.endpoint,
                                  "response_sha256": response.transport_sha256}}}}
        return SearchResult([hit], {"url": self.spec.endpoint}, _metadata(response),
            {"state": "public_unmetered", "requests": 1, "bytes": len(response.body), "requires_reconciliation": False}, raw_bytes=response.body)


class FredSource(FeedSource):
    def __init__(self, spec, transport, *, credential=None):
        super().__init__(spec, transport)
        self.credential = credential

    def available(self):
        return {"available": bool(self.credential), "reason": None if self.credential else "SOURCE_AUTH_REQUIRED"}

    def search(self, request: SearchRequest):
        if not self.credential:
            raise SourceError("SOURCE_AUTH_REQUIRED", delivery="not_sent")
        series = request.context.get("series_id") or self.spec.options.get("series_id")
        if not isinstance(series, str) or re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", series) is None:
            raise SourceError("SOURCE_IDENTITY_REQUIRED", delivery="not_sent", details={"reason": "FRED_SERIES_REQUIRED"})
        vintage = request.context.get("vintage_date") or request.end_date
        if not vintage:
            raise SourceError("TIME_UNVERIFIED", delivery="not_sent", details={"reason": "FRED_VINTAGE_REQUIRED"})
        params = {"series_id": series, "file_type": "json", "api_key": self.credential,
                  "realtime_start": vintage, "realtime_end": vintage}
        if request.start_date:
            params["observation_start"] = request.start_date
        if request.end_date:
            params["observation_end"] = request.end_date
        response = self.transport.request("GET", self.spec.endpoint, params=params,
                                          allowed_hosts=self.spec.allowed_hosts)
        payload = response.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("observations"), list)
                or any(not isinstance(row, dict) or not isinstance(row.get("date"), str)
                    or not isinstance(row.get("value"), str) for row in payload["observations"])):
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        public_params = {k: v for k, v in params.items() if k != "api_key"}
        public_url = self.spec.endpoint + "?" + urlencode(public_params)
        text = "series_id,vintage_date,observation_date,value\n" + "\n".join(
            series + "," + vintage + "," + str(r["date"]) + "," + str(r["value"]) for r in payload["observations"]
            if isinstance(r, dict) and "date" in r and "value" in r)
        authority = {"scope": "official_statistics", "series_ids": [series], "proof_type": "unverified",
            "proof_ref": {"source_id": self.spec.source_id, "source_url": public_url,
                          "response_sha256": response.transport_sha256, "vintage_date": vintage}}
        hit = {"url": public_url, "title": "FRED " + series + " vintage " + vintage,
            "snippet": "", "text": text, "text_kind": "data", "completeness": "complete",
            "published": source_time(vintage, evidence={"kind": "fred_vintage", "precision": "day"}),
            "raw_metadata": {"authority": authority, "response": safe_value(payload, (self.credential,)),
                             "intraday_publication_time_verified": False}}
        return SearchResult([hit], {"url": self.spec.endpoint, **public_params},
            _metadata(response, response=safe_value(payload, (self.credential,))),
            {"state": "public_unmetered", "requests": 1, "bytes": len(response.body), "requires_reconciliation": False},
            ["FRED day-level vintage does not prove intraday release availability"], response.body)


class CboeSource(FeedSource):
    def search(self, request: SearchRequest):
        response = self.transport.request("GET", self.spec.endpoint,
                                          allowed_hosts=self.spec.allowed_hosts)
        try:
            text = response.body.decode("utf-8-sig")
            reader = csv.DictReader(StringIO(text))
            fields = reader.fieldnames
            rows = list(reader)
            if not fields or "DATE" not in [x.upper() for x in fields] or not rows:
                raise ValueError
            for row in rows:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError
                for key, value in row.items():
                    if key.upper() == "DATE":
                        datetime.strptime(value, "%m/%d/%Y")
                    elif not math.isfinite(float(value)):
                        raise ValueError
        except (ValueError, UnicodeError, csv.Error):
            raise SourceError("CONTRACT_INVALID", delivery="returned", details={"reason": "CBOE_CSV_INVALID"}) from None
        series = self.spec.options.get("series_id", "VIX")
        authority = {"scope": "official_statistics", "series_ids": [series], "proof_type": "unverified",
            "proof_ref": {"source_id": self.spec.source_id, "source_url": self.spec.endpoint,
                          "response_sha256": response.transport_sha256}}
        hit = {"url": self.spec.endpoint, "title": "Cboe " + series + " historical observations",
            "snippet": "", "text": text, "text_kind": "data", "completeness": "complete",
            "published": source_time(None), "raw_metadata": {"authority": authority,
                "columns": fields, "row_count": len(rows), "last_observation": rows[-1],
                "intraday_publication_time_verified": False}}
        return SearchResult([hit], {"url": self.spec.endpoint}, _metadata(response),
            {"state": "public_unmetered", "requests": 1, "bytes": len(response.body), "requires_reconciliation": False}, raw_bytes=response.body)


class BlsCalendarSource(FeedSource):
    def search(self, request: SearchRequest):
        response = self.transport.request("GET", self.spec.endpoint,
            headers={"User-Agent": "WorkLoom-Tiger-Research/1.0"}, allowed_hosts=self.spec.allowed_hosts)
        try:
            text = response.body.decode("utf-8-sig")
        except UnicodeError:
            raise SourceError("CONTRACT_INVALID", delivery="returned") from None
        text = re.sub(r"\r?\n[ \t]", "", text)
        if "BEGIN:VCALENDAR" not in text:
            raise SourceError("CONTRACT_INVALID", delivery="returned")
        hits = []
        for block in re.findall(r"BEGIN:VEVENT\s*(.*?)\s*END:VEVENT", text, re.S):
            fields = {}
            for line in block.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key] = value
            start = next(((k, v) for k, v in fields.items() if k.startswith("DTSTART")), None)
            when = source_time(None)
            if start:
                key, value = start
                try:
                    if value.endswith("Z"):
                        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
                    elif "TZID=" in key:
                        zone = key.split("TZID=", 1)[1].split(";", 1)[0]
                        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(zone))
                    else:
                        parsed = None
                    when = source_time(parsed.isoformat() if parsed else None,
                        evidence={"kind": "announced_calendar", "raw": value, "url": self.spec.endpoint})
                except (ValueError, KeyError):
                    when = source_time(None, evidence={"kind": "calendar_unparsed", "raw": value})
            uid = fields.get("UID", "")
            url = fields.get("URL") or self.spec.endpoint + "#" + uid
            authority = {"scope": "official_policy", "series_ids": [], "proof_type": "official_release",
                "proof_ref": {"source_id": self.spec.source_id, "source_url": self.spec.endpoint,
                              "response_sha256": response.transport_sha256}}
            hits.append({"url": url, "title": fields.get("SUMMARY", uid), "snippet": fields.get("DESCRIPTION", ""),
                "text": "BEGIN:VEVENT\n" + block + "\nEND:VEVENT", "text_kind": "calendar",
                "completeness": "complete", "published": source_time(fields.get("DTSTAMP")),
                "raw_metadata": {"authority": authority, "event_time": when, "calendar_uid": uid,
                    "status": fields.get("STATUS", "CONFIRMED"), "future_event_is_not_realized": True}})
        return SearchResult(hits, {"url": self.spec.endpoint},
            _metadata(response, raw_response=text),
            {"state": "public_unmetered", "requests": 1, "bytes": len(response.body), "requires_reconciliation": False}, raw_bytes=response.body)
