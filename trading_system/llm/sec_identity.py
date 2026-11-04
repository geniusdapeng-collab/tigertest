"""Bind candidate filings to independently fetched, frozen SEC originals."""
from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
from html.parser import HTMLParser
import os
import re
from time import monotonic, sleep
from urllib.parse import urlsplit

import requests

from ..agent_evidence import strict_json
from .company_evidence import _EXCHANGE, _FILING_PATH, _FORM, _published_in_window, eligible_filings

SEC_COMPANY_CONTRACT = "sec-submission-original/v2"
SEC_VERIFY_METHOD = "sec.edgar.verify"
SEC_CONTACT_ENV = "TIGER_SEC_CONTACT"
_MAX_BYTES = 4 * 1024 * 1024
_ALLOWED_REASONS = {"contact_missing", "deadline", "transport", "http_access", "body_limit"}
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,100}@[A-Za-z0-9][A-Za-z0-9.-]{0,90}\.[A-Za-z]{2,24}")


def filing_request(url: str) -> dict:
    value = urlsplit(url)
    if (value.scheme != "https" or value.netloc not in {"sec.gov", "www.sec.gov"}
            or not _FILING_PATH.fullmatch(value.path) or value.query or value.fragment):
        raise ValueError("Invalid SEC filing URL")
    cik, accession, primary = value.path.split("/")[-3:]
    if int(cik) <= 0:
        raise ValueError("Invalid SEC issuer identity")
    cik = f"{int(cik):010d}"
    accession = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    return {"cik": cik, "accession": accession, "primary_document": primary,
            "submissions": f"https://data.sec.gov/submissions/CIK{cik}.json",
            "original": "https://www.sec.gov" + value.path}


def sec_verification_input(symbol, rows, run_date, cutoff):
    eligible = eligible_filings(symbol, rows, run_date, cutoff)
    filings = []
    for row in eligible:
        cik = urlsplit(row["url"]).path.split("/")[-3]
        if int(cik) > 0:
            filings.append({"id": row["id"], **filing_request(row["url"])})
    return {"symbol": symbol, "filings": filings}


def sec_body(url: str, body: bytes) -> dict:
    return {"url": url, "status": "observed", "body_base64": base64.b64encode(body).decode("ascii"),
            "sha256": hashlib.sha256(body).hexdigest()}


def _gap(url, reason):
    return {"url": url, "status": "unavailable", "reason": reason}


def _body(row, url):
    if not isinstance(row, dict) or row.get("url") != url:
        raise ValueError("SEC receipt endpoint differs")
    if row.get("status") == "unavailable":
        if set(row) != {"url", "status", "reason"} or row["reason"] not in _ALLOWED_REASONS:
            raise ValueError("Invalid SEC unavailability receipt")
        return None
    if row.get("status") != "observed" or set(row) != {"url", "status", "body_base64", "sha256"}:
        raise ValueError("Invalid SEC response receipt")
    encoded = row["body_base64"]
    if not isinstance(encoded, str) or len(encoded) > 4 * ((_MAX_BYTES + 2) // 3):
        raise ValueError("SEC receipt exceeds the body limit")
    body = base64.b64decode(encoded, validate=True)
    if (not body or len(body) > _MAX_BYTES or base64.b64encode(body).decode("ascii") != encoded
            or hashlib.sha256(body).hexdigest() != row["sha256"]):
        raise ValueError("SEC response digest differs")
    return body


class _FilingText(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.rows = []
        self.row = self.cell = None
        self.hidden = 0
        self.feed(body.decode("utf-8", errors="replace"))
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "ix:hidden"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag in {"p", "br", "div"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "ix:hidden"} and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        if tag in {"td", "th"} and self.row is not None and self.cell is not None:
            value = " ".join(" ".join(self.cell).split())
            if value:
                self.row.append(value)
            self.cell = None
            self.text.append(" ")
        elif tag == "tr" and self.row is not None:
            self.rows.append((len(self.text), self.row))
            self.row = self.cell = None
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.text.append(data.strip() + " ")
            if self.cell is not None:
                self.cell.append(data.strip())


def _original(body, symbol, form):
    parsed = _FilingText(body)
    cover = " ".join(" ".join(parsed.text[:300]).split()).casefold()
    if "securities and exchange commission" not in cover or "form " + form.casefold() not in cover:
        return None
    header = False
    for location, row in parsed.rows:
        if location > 300:
            break
        if ([word.casefold() for word in row] in (
                ["title of each class", "trading symbol(s)", "name of each exchange on which registered"],
                ["title of each class", "trading symbol", "name of each exchange on which registered"])):
            header = True
        elif header and len(row) == 3 and row[1] == symbol and _EXCHANGE.fullmatch(row[2]):
            return "\n".join(line for part in "".join(parsed.text).splitlines()
                             if (line := " ".join(part.split())))
    return None


def _submission(body, request, search_row, run_date, cutoff):
    try:
        value = strict_json(body)
        if f'{int(value["cik"]):010d}' != request["cik"]:
            return None
        recent = value["filings"]["recent"]
        matches = [index for index, accession in enumerate(recent["accessionNumber"])
                   if accession == request["accession"]]
        if len(matches) != 1:
            return None
        index = matches[0]
        form, primary, accepted, period, filed = (recent[field][index] for field in
            ("form", "primaryDocument", "acceptanceDateTime", "reportDate", "filingDate"))
        if (not _FORM.fullmatch("FORM " + form) or primary != request["primary_document"]
                or not _published_in_window(accepted, run_date, cutoff)
                or date.fromisoformat(filed) > run_date or date.fromisoformat(period) > run_date
                or datetime.fromisoformat(accepted.replace("Z", "+00:00")) >
                   datetime.fromisoformat(search_row["published"].replace("Z", "+00:00"))):
            return None
        return {"cik": request["cik"], "accession": request["accession"], "form": form,
                "report_period_end": period, "accepted_at": accepted, "filed_on": filed,
                "primary_document": primary}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        return None


def confirm_filings(request, rows, responses, run_date, cutoff):
    """Re-parse byte receipts offline and independently recover exactly owned sources."""
    if request != sec_verification_input(request["symbol"], rows, run_date, cutoff):
        raise ValueError("SEC request differs from the approved discovery results")
    if (set(responses) != {"filings"} or not isinstance(responses["filings"], list)
            or len(responses["filings"]) != len(request["filings"])):
        raise ValueError("SEC attempt coverage differs")
    by_id = {row["id"]: row for row in rows}
    documents, seen = [], set()
    unavailable = False
    for filing, response in zip(request["filings"], responses["filings"], strict=True):
        if set(response) != {"id", "submissions", "original"} or response["id"] != filing["id"]:
            raise ValueError("SEC filing attempt identity differs")
        submissions = _body(response["submissions"], filing["submissions"])
        if submissions is None:
            if response["original"] is not None:
                raise ValueError("SEC original must follow an observed submission")
            unavailable = True
            continue
        original = _body(response["original"], filing["original"])
        if original is None:
            unavailable = True
            continue
        row = by_id[filing["id"]]
        official = _submission(submissions, filing, row, run_date, cutoff)
        text = _original(original, request["symbol"], official["form"]) if official else None
        identity = (filing["cik"], filing["accession"])
        if not text or identity in seen:
            continue
        seen.add(identity)
        official.update(original_sha256=hashlib.sha256(original).hexdigest(),
                        submissions_sha256=hashlib.sha256(submissions).hexdigest())
        documents.append({"id": row["id"], "title": row["title"], "url": filing["original"],
                          "published_at": row["published"], "content": text,
                          "native_content_sha256": row["content_hash"], "sec_filing": official})
    status = ("filing_verified" if documents else "verification_unavailable" if unavailable
              else "no_eligible_filing" if rows else "no_results")
    outcome = {"status": status, "source_ids": [row["id"] for row in documents]}
    if documents:
        outcome["filing_evidence"] = [{"source_id": row["id"], **row["sec_filing"]} for row in documents]
    return outcome, documents


class SecTransport:
    """Only deterministic SEC hosts, with no redirect/retry and a finite read budget."""
    def __init__(self):
        contact = os.environ.get(SEC_CONTACT_ENV, "")
        self.contact = contact if _EMAIL.fullmatch(contact) else None
        self.remaining = 35.0
        self.last_call = 0.0
        self.access_blocked = False
        self.submissions = {}

    def _get(self, url, deadline):
        if not self.contact:
            return _gap(url, "contact_missing")
        if self.access_blocked:
            return _gap(url, "http_access")
        sleep(max(0, self.last_call + .2 - monotonic()))
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _gap(url, "deadline")
        self.last_call = monotonic()
        try:
            with requests.get(url, headers={"User-Agent": "Tiger research " + self.contact},
                              timeout=(min(3, remaining), min(4, remaining)),
                              allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    self.access_blocked = response.status_code in {403, 429}
                    return _gap(url, "http_access")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        return _gap(url, "body_limit")
                    if monotonic() >= deadline:
                        return _gap(url, "deadline")
                return sec_body(url, bytes(body)) if body else _gap(url, "transport")
        except requests.RequestException:
            return _gap(url, "transport")

    def observe(self, request):
        started = monotonic()
        deadline, results = started + self.remaining, []
        for filing in request["filings"]:
            submissions = filing["submissions"]
            if submissions not in self.submissions:
                self.submissions[submissions] = self._get(submissions, deadline)
            observed = self.submissions[submissions]
            original = self._get(filing["original"], deadline) if observed["status"] == "observed" else None
            results.append({"id": filing["id"], "submissions": observed, "original": original})
        self.remaining = max(0.0, self.remaining - (monotonic() - started))
        return {"filings": results}
