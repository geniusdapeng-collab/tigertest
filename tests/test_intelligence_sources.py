"""First-party parsers, raw-byte provenance, attachments and hostile-source cases."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
import socket
import zlib
from urllib.parse import urlsplit

import pytest
import requests

from trading_system.intelligence import (IntelligenceError, IntelligenceService, SourceRegistry,
    SourceSpec, build_plan, reconstruct_document, verify_bundle)
from trading_system.intelligence.adapters import SearchRequest
from trading_system.intelligence.contracts import SourceError
from trading_system.intelligence.direct import (BlsCalendarSource, CboeSource, FeedSource, FredSource,
                                                SecDirectorySource, SecSubmissionsSource)
from trading_system.intelligence.extract import extract_response
from trading_system.intelligence.transport import HttpResponse, HttpTransport
from test_intelligence_acquisition import NOW, plan, web


def _pdf_bytes(content_text="Revenue 10 million USD", *, compressed=False):
    """A self-contained PDF fixture whose text is known before extraction."""
    content = ("BT /F1 12 Tf 50 100 Td (" + content_text + ") Tj ET").encode()
    if compressed:
        content = zlib.compress(content)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + (b" /Filter /FlateDecode" if compressed else b"") + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (Quarterly supplement) >>"]
    body, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body)); body += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    start = len(body)
    body += b"xref\n0 7\n0000000000 65535 f \n" + b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets[1:])
    return body + b"trailer\n<< /Size 7 /Root 1 0 R /Info 6 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"


def _response(raw, media="application/pdf"):
    return HttpResponse("https://example.com/supplement.pdf", 200, {"content-type": media}, raw,
                        NOW.isoformat(), hashlib.sha256(raw).hexdigest())


class RoutedTransport(HttpTransport):
    """All bytes travel over the test server; emitted source URLs keep identity."""
    def __init__(self, local):
        super().__init__(allow_local=True)
        self.local = local
    def request(self, method, url, **kwargs):
        if url.startswith("https://www.sec.gov/Archives/"):
            routed = self.local + urlsplit(url).path
            kwargs["allowed_hosts"] = ()
            response = super().request(method, routed, **kwargs)
            return replace(response, url=url)
        return super().request(method, url, **kwargs)


def test_sec_directory_submissions_original_three_link_chain_and_replay(tmp_path, web):
    url, routes, calls = web
    accession = "0000320193-26-000123"
    path = "/Archives/edgar/data/320193/000032019326000123/aapl-20260930.htm"
    routes["/directory"] = (200, "application/json", {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
    routes["/submissions/CIK0000320193.json"] = (200, "application/json", {"cik": "0000320193", "name": "Apple Inc.", "filings": {"recent": {
        "accessionNumber": [accession], "form": ["10-Q"], "primaryDocument": ["aapl-20260930.htm"],
        "filingDate": ["2026-11-04"], "reportDate": ["2026-09-30"], "acceptanceDateTime": ["2026-11-04T15:00:00Z"]}, "files": []}})
    routes[path] = (200, "text/html", '<html><body><div>Apple Inc. CIK 0000320193 (AAPL)</div><div>Quarter ended September 30, 2026.</div><table><tr><td>Revenue</td><td>100 million USD</td></tr></table></body></html>')
    reg = SourceRegistry([
        SourceSpec("sec_directory", "sec_directory", url + "/directory", allowed_hosts=("127.0.0.1",),
                   capabilities=("discover", "identity"), minimum_interval_seconds=0),
        SourceSpec("sec_submissions", "sec_submissions", url + "/submissions/", allowed_hosts=("127.0.0.1", "www.sec.gov"),
                   authority_scopes=("sec_filing",), capabilities=("discover", "fetch", "attachments"), minimum_interval_seconds=0),
    ])
    transport = RoutedTransport(url)
    svc = IntelligenceService(tmp_path, reg, reg.build_adapters(transport=transport,
        environment={"TIGER_INTEL_SEC_USER_AGENT": "Fixture Research fixture@example.test"}), clock=lambda: NOW)
    p = plan(reg)
    job = svc.create_job(p, "sec-three-links")
    result = svc.collect(job["job_id"])
    filings = [d for d in result["documents"] if d["text_kind"] == "html_main"]
    assert len(filings) == 1
    filing = filings[0]
    assert filing["authority"]["proof_type"] == "sec_submission_acceptance"
    proof = filing["authority"]["proof_ref"]
    assert proof["issuer_directory_ref"]["cik"] == "0000320193"
    assert proof["accession"] == accession and proof["content_sha256"] == hashlib.sha256(filing["text"].encode()).hexdigest()
    assert filing["authority"]["issuer_id"] == "issuer:0000320193"
    assert result["entity_registry"]["entities"]["issuer:0000320193"]["name"] == "Apple Inc."
    assert len(calls) == 3 and all(c["headers"].get("User-Agent", "").startswith("Fixture Research") for c in calls)
    reconstructed = reconstruct_document(filing, svc.archive.read_blob, reg.public_manifest())
    assert reconstructed["text_sha256"] == proof["content_sha256"]
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert verify_bundle(tmp_path, frozen["bundle_id"])["bundle_id"] == frozen["bundle_id"]


def test_sec_missing_directory_cannot_promote_current_mapping_to_action(web):
    url, routes, _ = web
    routes["/submissions/CIK0000320193.json"] = (200, "application/json", {"cik": 320193, "name": "Apple Inc.", "filings": {"recent": {
        "accessionNumber": ["0000320193-26-000123"], "form": ["10-Q"], "primaryDocument": ["filing.htm"],
        "filingDate": ["2026-11-04"], "acceptanceDateTime": ["2026-11-04T15:00:00Z"]}}})
    spec = SourceSpec("sec_submissions", "sec_submissions", url + "/submissions/", allowed_hosts=("127.0.0.1",), authority_scopes=("sec_filing",))
    source = SecSubmissionsSource(spec, HttpTransport(allow_local=True), user_agent="Fixture fixture@example.test")
    result = source.search(SearchRequest("AAPL", context={"cik": "320193"}))
    assert result.hits[0]["raw_metadata"]["authority"]["proof_type"] == "unverified"


def test_sec_historical_files_are_separate_budgeted_attempts_with_parent_replay(tmp_path, web):
    url, routes, calls = web
    historical_name = "CIK0000320193-submissions-001.json"
    routes["/directory"] = (200, "application/json", {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
    routes["/submissions/CIK0000320193.json"] = (200, "application/json", {"cik": 320193, "name": "Apple Inc.",
        "filings": {"recent": {"accessionNumber": []}, "files": [{"name": historical_name,
            "filingFrom": "2025-01-01", "filingTo": "2025-12-31", "filingCount": 1}]}})
    routes["/submissions/" + historical_name] = (200, "application/json", {
        "accessionNumber": ["0000320193-25-000001"], "form": ["10-K"], "primaryDocument": ["annual.htm"],
        "filingDate": ["2025-11-04"], "acceptanceDateTime": ["2025-11-04T15:00:00Z"]})
    routes["/Archives/edgar/data/320193/000032019325000001/annual.htm"] = (200, "text/html",
        "<html><body>Apple Inc. annual financial statements revenue 10 million USD.</body></html>")
    reg = SourceRegistry([
        SourceSpec("sec_directory", "sec_directory", url + "/directory", allowed_hosts=("127.0.0.1",),
            capabilities=("discover", "identity"), minimum_interval_seconds=0),
        SourceSpec("sec_submissions", "sec_submissions", url + "/submissions/", allowed_hosts=("127.0.0.1", "www.sec.gov"),
            authority_scopes=("sec_filing",), minimum_interval_seconds=0)])
    svc = IntelligenceService(tmp_path, reg, reg.build_adapters(transport=RoutedTransport(url),
        environment={"TIGER_INTEL_SEC_USER_AGENT": "Fixture fixture@example.test"}), clock=lambda: NOW)
    p = plan(reg); job = svc.create_job(p, "historical"); acquired = svc.collect(job["job_id"])
    assert len(calls) == 4 and acquired["usage"]["requests"] == 4
    assert sorted(a["operation"] for a in acquired["attempts"]) == ["fetch", "search", "search", "search"]
    filing = next(d for d in acquired["documents"] if d["text_kind"] == "html_main")
    assert filing["authority"]["proof_ref"]["submissions_file"] == historical_name
    assert filing["title"] == "Apple Inc. 10-K 2025-11-04"
    reconstruct_document(filing, svc.archive.read_blob, reg.public_manifest())
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"],
        expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert verify_bundle(tmp_path, frozen["bundle_id"])["bundle_id"] == frozen["bundle_id"]


@pytest.mark.parametrize("filename", ["../CIK0000320193-submissions-001.json", "CIK0000789019-submissions-001.json", "https://evil.test/a.json"])
def test_sec_historical_file_path_cannot_escape_issuer(web, filename):
    source = SecSubmissionsSource(SourceSpec("sec_submissions", "sec_submissions", web[0] + "/submissions/"),
        HttpTransport(allow_local=True), user_agent="Fixture fixture@example.test")
    with pytest.raises(SourceError) as error:
        source.search(SearchRequest("AAPL", context={"cik": "320193",
            "submissions_file": {"name": filename, "parent_ref": {"raw_response_ref": {}}}}))
    assert error.value.code == "CONTRACT_INVALID" and not web[2]


def test_malformed_sec_history_window_and_cboe_numeric_data_fail_explicitly(web):
    url, routes, _ = web
    routes["/submissions/CIK0000320193.json"] = (200, "application/json", {"cik": 320193,
        "filings": {"recent": {"accessionNumber": []}, "files": [{"name": "CIK0000320193-submissions-001.json",
            "filingFrom": "2025-01-01", "filingTo": None}]}})
    source = SecSubmissionsSource(SourceSpec("sec_submissions", "sec_submissions", url + "/submissions/"),
        HttpTransport(allow_local=True), user_agent="Fixture fixture@example.test")
    with pytest.raises(SourceError) as error:
        source.search(SearchRequest("AAPL", context={"cik": "320193"}))
    assert error.value.code == "CONTRACT_INVALID"
    routes["/vix"] = (200, "text/csv", "DATE,OPEN,HIGH,LOW,CLOSE\n11/04/2026,NaN,22,20,21\n")
    with pytest.raises(SourceError) as error:
        CboeSource(SourceSpec("cboe_vix", "cboe_csv", url + "/vix"), HttpTransport(allow_local=True)).search(SearchRequest("vix"))
    assert error.value.code == "CONTRACT_INVALID"


@pytest.mark.parametrize("kind,source_id", [("sec_directory", "sec_directory"), ("sec_submissions", "sec_submissions"), ("feed", "sec_current")])
def test_sec_identity_is_required_before_any_network(web, kind, source_id):
    spec = SourceSpec(source_id, kind, web[0] + "/source", allowed_hosts=("www.sec.gov", "127.0.0.1"))
    cls = {"sec_directory": SecDirectorySource, "sec_submissions": SecSubmissionsSource, "feed": FeedSource}[kind]
    source = cls(spec, HttpTransport(allow_local=True))
    assert source.available()["available"] is False
    with pytest.raises(SourceError) as error:
        source.search(SearchRequest("AAPL", context={"cik": "320193"}))
    assert error.value.code == "SOURCE_IDENTITY_REQUIRED" and not web[2]


def test_atom_rss_ir_identity_raw_publication_and_main_body(web):
    url, routes, calls = web
    routes["/feed"] = (200, "application/atom+xml", '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>release-1</id><title>Quarterly Results</title><published>2026-11-04T10:00:00-05:00</published><link href="' + url + '/release"/><summary>Official discovery description</summary></entry></feed>')
    spec = SourceSpec("ir_apple", "issuer_rss", url + "/feed", allowed_hosts=("127.0.0.1",),
        authority_scopes=("issuer_ir",), options={"issuer_id": "issuer:0000320193"})
    source = FeedSource(spec, HttpTransport(allow_local=True))
    result = source.search(SearchRequest("earnings"))
    assert result.hits[0]["published"]["value"] == "2026-11-04T15:00:00+00:00"
    assert result.hits[0]["raw_metadata"]["authority"]["issuer_id"] == "issuer:0000320193"
    assert result.raw_bytes and len(calls) == 1


def test_feed_external_entities_are_rejected(web):
    url, routes, _ = web
    routes["/feed"] = (200, "application/xml", '<!DOCTYPE foo [<!ENTITY evil SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&evil;</title></item></channel></rss>')
    source = FeedSource(SourceSpec("feed", "feed", url + "/feed"), HttpTransport(allow_local=True))
    with pytest.raises(SourceError) as error:
        source.search(SearchRequest("news"))
    assert error.value.code == "CONTRACT_INVALID"


def test_fred_vintage_and_unknown_intraday_time_are_explicit(web):
    url, routes, calls = web
    routes["/fred"] = (200, "application/json", {"realtime_start": "2026-11-04", "realtime_end": "2026-11-04",
        "observations": [{"date": "2026-10-01", "value": "4.00", "realtime_start": "2026-11-04", "realtime_end": "2026-11-04"}]})
    spec = SourceSpec("fred", "fred", url + "/fred", allowed_hosts=("127.0.0.1",),
        authority_scopes=("official_statistics",), options={"series_id": "FEDFUNDS"})
    result = FredSource(spec, HttpTransport(allow_local=True), credential="fixture-fred-secret").search(
        SearchRequest("rates", start_date="2026-01-01", end_date="2026-11-04"))
    assert "realtime_start=2026-11-04" in calls[0]["path"] and "realtime_end=2026-11-04" in calls[0]["path"]
    assert result.hits[0]["published"]["precision"] == "date"
    assert result.hits[0]["raw_metadata"]["intraday_publication_time_verified"] is False
    assert "fixture-fred-secret" not in json.dumps(result.to_dict())


def test_cboe_data_is_complete_numeric_source_not_news_timestamp(web):
    url, routes, _ = web
    routes["/vix"] = (200, "text/csv", 'DATE,OPEN,HIGH,LOW,CLOSE\n11/03/2026,20,21,19,20.2\n11/04/2026,20.2,22,20,21\n')
    source = CboeSource(SourceSpec("cboe_vix", "cboe_csv", url + "/vix", options={"series_id": "VIX"}), HttpTransport(allow_local=True))
    result = source.search(SearchRequest("vix"))
    assert result.hits[0]["text_kind"] == "data" and result.hits[0]["completeness"] == "complete"
    assert result.hits[0]["published"]["precision"] == "unknown"
    assert result.hits[0]["raw_metadata"]["row_count"] == 2


def test_bls_calendar_dst_and_cancellation_are_not_realized_data(web):
    url, routes, _ = web
    routes["/bls"] = (200, "text/calendar", 'BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:jobs\nDTSTART;TZID=America/New_York:20261106T083000\nSUMMARY:Employment Situation\nSTATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR\n')
    source = BlsCalendarSource(SourceSpec("bls_calendar", "bls_calendar", url + "/bls"), HttpTransport(allow_local=True))
    result = source.search(SearchRequest("jobs"))
    event = result.hits[0]["raw_metadata"]
    assert event["event_time"]["value"] == "2026-11-06T13:30:00+00:00"
    assert event["future_event_is_not_realized"] and event["status"] == "CANCELLED"


def test_html_attachment_full_bytes_and_paywall_completeness():
    raw = b'<html><head><script type="application/ld+json">{"isAccessibleForFree":false,"datePublished":"2026-11-04"}</script></head><body><article><h1>Results</h1><p>Preview only.</p><a href="/report.pdf">Financial supplement</a></article></body></html>'
    response = HttpResponse("https://example.com/release", 200, {"content-type": "text/html"}, raw,
                            NOW.isoformat(), hashlib.sha256(raw).hexdigest())
    result = extract_response(response)
    assert result.raw_bytes == raw and result.completeness == "partial"
    assert result.attachments == [{"url": "https://example.com/report.pdf", "label": "Financial supplement"}]
    assert result.published["precision"] == "date"
    assert result.usage["state"] == "public_unmetered" and not result.usage["requires_reconciliation"]


def test_pdf_original_bytes_text_page_locations_and_encrypted_or_invalid_paths():
    raw = _pdf_bytes(); parsed = extract_response(_response(raw))
    assert parsed.raw_bytes == raw and parsed.text_kind == "pdf_text" and parsed.completeness == "complete"
    assert parsed.title == "Quarterly supplement" and parsed.text == "Revenue 10 million USD"
    assert parsed.metadata["page_locations"] == [{"page": 1, "start": 0, "end": len(parsed.text)}]
    from pypdf import PdfWriter
    writer = PdfWriter(clone_from=BytesIO(raw)); writer.encrypt("fixture-password")
    encrypted = BytesIO(); writer.write(encrypted)
    for body in (encrypted.getvalue(), b"%PDF-1.4\nmalformed"):
        result = extract_response(_response(body))
        assert result.raw_bytes == body and result.text == "" and result.completeness == "partial"
        assert result.metadata["extraction_error"] == "PDF_PARSE_FAILED", result._parser_runtime
    unsupported = extract_response(_response(b"\xff\xfe\x00\x81", "application/octet-stream"))
    assert unsupported.raw_bytes == b"\xff\xfe\x00\x81" and unsupported.completeness == "partial"


def test_original_pdf_attachment_replay_does_not_inherit_parent_publication(tmp_path, web):
    from test_intelligence_acquisition import service
    svc, reg, _ = service(tmp_path, web)
    web[1]["/article"] = (200, "text/html", '<article><meta property="article:published_time" content="2026-11-04T15:00:00Z"><h1>Release</h1><a href="/supplement.pdf">Quarterly supplement</a></article>')
    web[1]["/supplement.pdf"] = (200, "application/pdf", _pdf_bytes())
    p = plan(reg); job = svc.create_job(p, "pdf-attachment"); acquired = svc.collect(job["job_id"])
    attachment = next(d for d in acquired["documents"] if d["text_kind"] == "pdf_text")
    assert attachment["times"]["published"]["value"] is None
    assert attachment["times"]["published"]["evidence"] == {}
    reconstruct_document(attachment, svc.archive.read_blob, reg.public_manifest())
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    assert len(verify_bundle(tmp_path, frozen["bundle_id"])["documents"]) == 3


def test_dns_rebinding_cannot_replace_pinned_socket_target(monkeypatch):
    lookups = []
    def resolve(host, port, *args, **kwargs):
        lookups.append(host)
        ip = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    transport = HttpTransport()
    transport._validate("https://example.com/release")
    adapter = transport.session.get_adapter("https://example.com/release")
    request = requests.Request("GET", "https://example.com/release").prepare()
    pool = adapter.get_connection_with_tls_context(request, True, proxies={})
    assert pool.host == "93.184.216.34"
    assert pool.conn_kw["server_hostname"] == "example.com" and pool.assert_hostname == "example.com"
    assert lookups == ["example.com"]
    proxied = requests.Request("GET", "http://example.com/release").prepare()
    assert adapter.request_url(proxied, {"http": "http://127.0.0.1:7890"}) == "http://93.184.216.34/release"
    with pytest.raises(SourceError):
        transport._validate("https://example.com/next")


@pytest.mark.parametrize("kind", ["compressed_pdf", "entity_html", "mixed_html", "entity_metadata"])
def test_decoded_credential_echo_is_refused_before_any_blob(tmp_path, web, kind):
    from test_intelligence_acquisition import service
    from pypdf import PdfReader
    svc, _, _ = service(tmp_path, web)
    secret = "fixture-secret"
    encoded = "".join("&#" + str(ord(character)) + ";" for character in secret)
    if kind == "compressed_pdf":
        raw, media = _pdf_bytes(secret, compressed=True), "application/pdf"
        assert secret.encode() not in raw and secret in PdfReader(BytesIO(raw)).pages[0].extract_text()
    else:
        content = ("<article>Public " + (secret + " " if kind == "mixed_html" else "") + encoded + "</article>")
        if kind == "entity_metadata":
            content = '<meta name="description" content="' + encoded + '"><article>Public body</article>'
        raw, media = content.encode(), "text/html"
    response = HttpResponse("https://example.com/public", 200, {"content-type": media}, raw,
        NOW.isoformat(), hashlib.sha256(raw).hexdigest(), request_count=2)
    extracted = extract_response(response)
    assert secret in extracted.text or secret in json.dumps(extracted.metadata)
    with pytest.raises(SourceError) as error:
        svc._observe_result("web_page", "fetch", {"url": response.url}, extracted)
    assert error.value.code == "SOURCE_CREDENTIAL_ECHO" and error.value.delivery == "returned"
    assert error.value.bytes_received == len(raw) and error.value.details["request_count"] == 2
    assert secret not in str(error.value) and secret not in json.dumps(error.value.to_dict())
    assert not [path for path in (tmp_path / "blobs").rglob("*") if path.is_file()]


@pytest.mark.parametrize("echo", [False, True])
def test_actual_compressed_pdf_collect_rejects_echo_and_preserves_clean_header_control(tmp_path, web, echo):
    from test_intelligence_acquisition import service
    from pypdf import PdfReader
    svc, reg, _ = service(tmp_path, web)
    content = "fixture-secret" if echo else "Revenue 10 million USD"
    raw = _pdf_bytes(content, compressed=True)
    assert b"fixture-secret" not in raw
    web[1]["/article"] = (302, "text/plain", b"", {"Location": web[0] + "/compressed.pdf"})
    web[1]["/compressed.pdf"] = (200, "application/pdf", raw, {"ETag": "fixture-secret"})
    p = plan(reg); job = svc.create_job(p, "compressed-pdf-echo"); acquired = svc.collect(job["job_id"])
    assert acquired["usage"]["requests"] == 3 and len(web[2]) == 3
    fetch = next(attempt for attempt in acquired["attempts"] if attempt["operation"] == "fetch")
    assert fetch["result"]["request_count"] == 2 and fetch["result"]["bytes_received"] == len(raw)
    blobs = [path.read_bytes() for path in (tmp_path / "blobs").rglob("*") if path.is_file()]
    assert all(b"fixture-secret" not in body for body in blobs)
    pdf_blobs = [body for body in blobs if body.startswith(b"%PDF-")]
    assert all("fixture-secret" not in PdfReader(BytesIO(body)).pages[0].extract_text() for body in pdf_blobs)
    if echo:
        assert pdf_blobs == [] and acquired["gaps"][0]["reason"] == "SOURCE_CREDENTIAL_ECHO"
        assert fetch["status"] == "failed" and fetch["result"]["error"]["details"]["request_count"] == 2
        assert "fixture-secret" not in json.dumps(fetch["result"]["error"])
    else:
        assert pdf_blobs == [raw]
        document = next(doc for doc in acquired["documents"] if doc["text_kind"] == "pdf_text")
        assert document["text"] == content and document["completeness"] == "complete"
        assert document["normalization"]["http_headers"]["etag"] == "[REDACTED_SECRET]"
    frozen = svc.freeze(job["job_id"], expected_plan_hash=p["plan_hash"], expected_revision=svc.status(job["job_id"])["revision"], expected_frontier_hash=p["frontier_manifest_hash"])
    verify_bundle(tmp_path, frozen["bundle_id"])
