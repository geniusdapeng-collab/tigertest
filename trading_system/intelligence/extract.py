"""Keep original bytes, separate article text from navigation and locate attachments."""
from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from io import BytesIO
import json
import re
from urllib.parse import urljoin, urlsplit

from .contracts import FetchResult, SourceError, digest, source_time
from .transport import HttpResponse

_REDACTED_LINK_POLICY = "tiger.redacted-html-links/v1"
_REDACTION_MARKERS = ("[REDACTED_URL]", "[REDACTED_SECRET]", "[REDACTED_TOKEN]")


class _Page(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.stack, self.skip = url, [], 0
        self.all, self.main, self.title, self.heading = [], [], [], []
        self.main_depth = 0
        self.title_depth = self.h1_depth = 0
        self.meta, self.attachments, self.jsonld = {}, [], []
        self.script_type, self.script_data, self.anchor = None, [], None
        self.redacted_links_omitted = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.stack.append(tag)
        if tag in {"main", "article"}:
            self.main_depth += 1
        if tag in {"nav", "footer", "header", "aside", "form", "button", "style", "script", "noscript"}:
            self.skip += 1
        if tag == "script":
            self.script_type = attrs.get("type")
            self.script_data = []
        if tag == "title":
            self.title_depth += 1
        if tag == "h1":
            self.h1_depth += 1
        if tag == "meta":
            name = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
            if name and attrs.get("content"):
                self.meta[name.lower()] = attrs["content"]
        if tag == "a":
            self.anchor = None
            if attrs.get("href"):
                if any(marker in attrs["href"] for marker in _REDACTION_MARKERS):
                    self.redacted_links_omitted = True
                else:
                    self.anchor = {"url": urljoin(self.url, attrs["href"]), "label": ""}
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "br", "section"}:
            self._add("\n")
        if tag in {"td", "th"}:
            self._add(" | ")

    def handle_endtag(self, tag):
        if tag == "script":
            if self.script_type == "application/ld+json":
                try:
                    self.jsonld.append(json.loads("".join(self.script_data)))
                except (ValueError, RecursionError):
                    self.meta["jsonld_parse_error"] = "true"
            self.script_type, self.script_data = None, []
        if tag == "a" and self.anchor:
            path = urlsplit(self.anchor["url"]).path.lower()
            if (path.endswith((".pdf", ".csv", ".xlsx", ".xls", ".xml"))
                    or re.search(r"exhibit\s*(?:99|10)|earnings\s+release|financial\s+supplement", self.anchor["label"], re.I)):
                self.attachments.append(self.anchor)
            self.anchor = None
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag == "h1" and self.h1_depth:
            self.h1_depth -= 1
        if tag in {"nav", "footer", "header", "aside", "form", "button", "style", "script", "noscript"} and self.skip:
            self.skip -= 1
        if tag in {"main", "article"} and self.main_depth:
            self.main_depth -= 1
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "section"}:
            self._add("\n")
        if self.stack:
            self.stack.pop()

    def _add(self, text):
        if not self.skip:
            self.all.append(text)
            if self.main_depth:
                self.main.append(text)

    def handle_data(self, text):
        if self.script_type is not None:
            self.script_data.append(text)
        if self.title_depth:
            self.title.append(text)
        if self.h1_depth and not self.skip:
            self.heading.append(text)
        if self.anchor:
            self.anchor["label"] += text
        self._add(text)


def _clean(text):
    return "\n".join(line for raw in text.splitlines()
                     if (line := re.sub(r"[ \t\r\f\v]+", " ", raw).strip()))


def _date_fields(value, found=None):
    found = {} if found is None else found
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"datePublished", "dateModified"} and isinstance(child, str):
                found.setdefault(key, child)
            elif isinstance(child, (dict, list)):
                _date_fields(child, found)
    elif isinstance(value, list):
        for item in value:
            _date_fields(item, found)
    return found


def _extract_response_inner(response: HttpResponse, *, original_url: str | None = None,
                            trusted_filing: bool = False, max_pages: int | None = None,
                            max_text_bytes: int | None = None) -> FetchResult:
    media = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
    body, url = response.body, response.url
    metadata = {"http_status": response.status, "http_headers": response.headers,
                "transport_sha256": response.transport_sha256,
                "request_count": response.request_count,
                "retrieved_at": response.received_at, "extractor": "tiger-main-text/v1"}
    def finish(*args, **kwargs):
        return FetchResult(*args, **kwargs, usage={"state": "public_unmetered",
            "requests": response.request_count, "bytes": len(body), "requires_reconciliation": False})
    if media == "application/pdf" or body.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader
        except ImportError:
            return finish(original_url or url, url, "", "", body, "application/pdf",
                "attachment", "partial", metadata={**metadata, "extraction_error": "PDF_DEPENDENCY_UNAVAILABLE"})
        try:
            pdf = PdfReader(BytesIO(body), strict=True)
            if pdf.is_encrypted:
                raise ValueError
            if max_pages is not None and len(pdf.pages) > max_pages:
                return finish(original_url or url, url, "", "", body, "application/pdf",
                    "attachment", "partial", metadata={**metadata, "extraction_error": "PARSER_PAGE_LIMIT"})
            pages, offset, locations, text_bytes = [], 0, [], 0
            for index, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                text_bytes += len(text.encode()) + 2
                if max_text_bytes is not None and text_bytes > max_text_bytes:
                    return finish(original_url or url, url, "", "", body, "application/pdf",
                        "attachment", "partial", metadata={**metadata, "extraction_error": "PARSER_TEXT_LIMIT"})
                pages.append(text)
                locations.append({"page": index, "start": offset, "end": offset + len(text)})
                offset += len(text) + 2
            text = "\n\n".join(pages)
            title = str((pdf.metadata or {}).get("/Title", ""))
            return finish(original_url or url, url, title, text, body, "application/pdf",
                "pdf_text", "complete" if text.strip() else "partial",
                metadata={**metadata, "page_locations": locations,
                          **({"extraction_error": "IMAGE_ONLY_PDF"} if not text.strip() else {})})
        except Exception:
            return finish(original_url or url, url, "", "", body, "application/pdf",
                "attachment", "partial", metadata={**metadata, "extraction_error": "PDF_PARSE_FAILED"})
    try:
        header = response.headers.get("content-type", "")
        charset = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", header, re.I)
        encoding = charset[1] if charset else "utf-8-sig"
        text = body.decode(encoding)
    except (UnicodeError, LookupError):
        return finish(original_url or url, url, "", "", body, media, "attachment", "partial",
            metadata={**metadata, "extraction_error": "SOURCE_ENCODING_OR_FORMAT_UNSUPPORTED"})
    if media in {"text/html", "application/xhtml+xml"} or re.search(r"<(?:html|article|main)\b", text[:4000], re.I):
        parser = _Page(url)
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=len(body),
                              details={"reason": "HTML_PARSE_FAILED"}) from None
        main = _clean("".join(parser.main))
        content = main or _clean("".join(parser.all))
        dates = _date_fields(parser.jsonld)
        published = parser.meta.get("article:published_time") or dates.get("datePublished") or parser.meta.get("datepublished")
        updated = parser.meta.get("article:modified_time") or dates.get("dateModified") or parser.meta.get("datemodified")
        attachments = list({row["url"]: row for row in parser.attachments}.values())
        if parser.redacted_links_omitted:
            # The same deterministic rule runs during source normalization
            # and offline replay. Placeholders never become relative GETs.
            metadata.update(redaction_markup_policy=_REDACTED_LINK_POLICY,
                extractor_config_sha256=digest({"engine": "builtin", "redaction_markup_policy": _REDACTED_LINK_POLICY}))
        paywall = any('"isAccessibleForFree": false' in json.dumps(item) for item in parser.jsonld)
        complete = bool(content) and (bool(main) or trusted_filing) and not paywall
        return finish(original_url or url, url,
            _clean(" ".join(parser.heading or parser.title)), content, body, media,
            "html_main" if main or trusted_filing else "html_text", "complete" if complete else "partial" if paywall else "unknown",
            source_time(published, evidence={"kind": "page_metadata", "url": url}),
            source_time(updated, evidence={"kind": "page_metadata", "url": url}),
            {**metadata, "page_metadata": parser.meta, "main_element_found": bool(main),
             "trusted_filing_extraction": trusted_filing, "explicit_paywall": paywall,
             **({"extraction_error": "EMPTY_BODY"} if not content.strip() else {})}, attachments)
    kind = "data" if media in {"application/json", "text/csv", "application/csv", "application/xml", "text/xml", "text/calendar"} else "plain_text"
    return finish(original_url or url, url, "", text, body, media, kind, "complete" if text.strip() else "partial",
                       metadata={**metadata, **({"extraction_error": "EMPTY_BODY"} if not text.strip() else {})})


def extract_response(response: HttpResponse, *, original_url: str | None = None,
                     trusted_filing: bool = False, policy=None, deadline=None) -> FetchResult:
    """Preserve legacy normalization while isolating expensive/untrusted parsers.

    Successful default extraction keeps its existing deterministic recipe, so
    historical evidence replays without silently changing its schema.
    """
    from .parse_limits import ParsePolicy, isolated_extract
    selected = ParsePolicy.from_value(policy)
    if response.parse_failure_receipt is not None:
        from .parse_limits import replay_partial
        return replay_partial(response, original_url=original_url, policy=selected,
                              receipt=response.parse_failure_receipt)
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if policy is not None or media == "application/pdf" or response.body.startswith(b"%PDF-"):
        return isolated_extract(response, original_url=original_url, trusted_filing=trusted_filing,
                                policy=selected, deadline=deadline)
    return _extract_response_inner(response, original_url=original_url, trusted_filing=trusted_filing)
