"""Point-in-time corroboration for the required-model research profile."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .credibility import CrossValidationStats, SourceTier, _canonical_event, evidence_for
from .models import CleanDocument

EVENT_CONTRACT = "point-in-time-event/v1"
_EVENT_MAX_AGE = timedelta(days=90)
_MARKET_ZONE = ZoneInfo("America/New_York")
_PUBLISHERS = {
    "reuters": "reuters.com", "bloomberg": "bloomberg.com", "associated press": "apnews.com",
    "dow jones": "wsj.com", "financial times": "ft.com", "cnbc": "cnbc.com",
}
_DIRECT_HOSTS = {*_PUBLISHERS.values(), "nytimes.com", "federalregister.gov"}
# Reviewed issuer-controlled investor pages. A web mention on a generic
# corporate host or an unrelated product path is not an issuer original.
_ISSUER_IR = {
    "investor.apple.com": ("AAPL", "/"),
    "www.apple.com": ("AAPL", "/newsroom/"),
    "www.microsoft.com": ("MSFT", "/en-us/investor/"),
    "investor.nvidia.com": ("NVDA", "/"),
    "ir.tesla.com": ("TSLA", "/"),
}
_WIRE = "|".join(re.escape(name) for name in _PUBLISHERS)
_ATTRIBUTION = re.compile(
    r"(?:\b(?:reporting by|reported by|originally published by|syndicated from|via|according to)\s+("
    + _WIRE + r")\b|\(\s*(" + _WIRE + r")\s*\)|(?:^|\n)\s*By\s+("
    + _WIRE + r")\b|©\s*(?:20\d{2}\s+)?(" + _WIRE + r")\b)", re.IGNORECASE)
_ISO_DAY = r"20\d{2}-\d{2}-\d{2}"
_LONG_DAY = r"[A-Za-z]{3,9}\s+\d{1,2},?\s+20\d{2}"
_NAMED_PERIOD = re.compile(
    r"\b(?:quarter|fiscal year|(?:reporting )?period|year)\s+(?:ended|ending)\s+("
    + _ISO_DAY + "|" + _LONG_DAY + r")\b", re.IGNORECASE)
_FISCAL_QUARTER = re.compile(r"\b(?:Q([1-4])\s*(?:FY\s*)?(20\d{2})|(?:FY\s*)?(20\d{2})\s*Q([1-4]))\b", re.IGNORECASE)
_EFFECTIVE = re.compile(
    r"\b(?:effective(?:\s+(?:as of|on|date))?|event date)\s*[:=]?\s*("
    + _ISO_DAY + "|" + _LONG_DAY + r")\b", re.IGNORECASE)
_ACCESSION = re.compile(r"\b(?:SEC\s+)?accession(?:\s+(?:number|no\.?))?\s*[:#]?\s*(\d{10}-\d{2}-\d{6})\b",
                        re.IGNORECASE)
_EDGAR_PATH = re.compile(r"/Archives/edgar/data/(\d{1,10})/(\d{18})/[^/]+")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}")


def _publication(value: str, cutoff: datetime) -> str | None:
    value = value.strip()
    if re.fullmatch(_ISO_DAY, value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return "unknown_publication"
        market_day = cutoff.astimezone(_MARKET_ZONE).date()
        if day > market_day:
            return "future_publication"
        if day == market_day:
            return "unknown_publication"
        return "stale_publication" if market_day - day > _EVENT_MAX_AGE else None
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            published = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return "unknown_publication"
    if published is None or published.utcoffset() is None:
        return "unknown_publication"
    if published > cutoff:
        return "future_publication"
    return "stale_publication" if cutoff - published > _EVENT_MAX_AGE else None


def _period_day(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value.replace(",", ""), fmt).date()
        except ValueError:
            continue
    return None


def _issuer_ir(url: str) -> str | None:
    value = urlsplit(url)
    reviewed = _ISSUER_IR.get(value.netloc)
    if value.scheme == "https" and reviewed and value.path.startswith(reviewed[1]):
        return reviewed[0]
    return None


def _filing_issuer(doc: CleanDocument) -> str | None:
    value = urlsplit(doc.raw.url)
    if (value.scheme != "https" or value.netloc not in {"sec.gov", "www.sec.gov"}
            or not _EDGAR_PATH.fullmatch(value.path) or len(set(doc.tickers)) != 1):
        return None
    from ..llm.company_evidence import _issuer_cover

    ticker = doc.tickers[0]
    return ticker if _issuer_cover(doc.raw.content, ticker) else None


def _identity(doc: CleanDocument, canon: str, text: str, cutoff: datetime,
              security_ids: dict[str, str] | None, issuer: str | None) -> tuple[str, ...] | None:
    symbols = sorted({symbol for symbol in doc.tickers if _SYMBOL.fullmatch(symbol)
                      and re.search(r"(?:\$|\(|NYSE:|NASDAQ:)" + re.escape(symbol) + r"(?![\w.\-])"
                                    if len(symbol) == 1 else
                                    r"(?<![\w.\-])" + re.escape(symbol) + r"(?![\w.\-])", text)})
    if issuer is not None:
        if set(doc.tickers) - {issuer}:
            return None
        symbols = [issuer]
    elif not symbols or len(symbols) != len(set(doc.tickers)):
        return None
    if security_ids is not None and any(symbol not in security_ids for symbol in symbols):
        return None
    entity = ",".join(sorted(security_ids[symbol] if security_ids is not None
                             else "us:" + symbol for symbol in symbols))
    if canon in {"earnings", "guidance"}:
        days = {_period_day(match) for match in _NAMED_PERIOD.findall(text)}
        if days:
            if len(days) != 1 or None in days:
                return None
            day = next(iter(days))
            if canon == "earnings" and day > cutoff.astimezone(_MARKET_ZONE).date():
                return None
            return (entity, canon, day.isoformat())
        quarters = {f"{year or trailing_year}-Q{first or last}"
                    for first, year, trailing_year, last in _FISCAL_QUARTER.findall(text)}
        if len(quarters) == 1:
            return (entity, canon, next(iter(quarters)))
    accessions = {match.replace("-", "") for match in _ACCESSION.findall(text)}
    parsed = urlsplit(doc.raw.url)
    official = _EDGAR_PATH.fullmatch(parsed.path) if parsed.hostname in {"sec.gov", "www.sec.gov"} else None
    if official:
        accessions.add(official[2])
    if len(accessions) == 1:
        return (entity, canon, next(iter(accessions)))
    if accessions or canon in {"earnings", "guidance"}:
        return None
    dates = {_period_day(item) for item in _EFFECTIVE.findall(text)}
    if len(dates) == 1 and None not in dates:
        # Use the subject as well as the effective day: two different
        # events involving the same company may take effect together.
        subject = re.sub(r"[\W_]+", " ", _EFFECTIVE.sub("", doc.raw.title).casefold()).strip()
        if subject:
            day = next(iter(dates)).isoformat()
            return (entity, canon, day, hashlib.sha256(subject.encode()).hexdigest()[:24])
    return None


def _origin(doc: CleanDocument, issuer: str | None) -> tuple[str, bool]:
    if issuer is not None:
        return "issuer:" + issuer, True
    host = (urlsplit(doc.raw.url).hostname or "").lower().rstrip(".")
    domain = next((suffix for suffix in sorted(_DIRECT_HOSTS)
                   if host == suffix or host.endswith("." + suffix)), None)
    origins = {_PUBLISHERS[next(item for item in groups if item).lower()]
               for groups in _ATTRIBUTION.findall(doc.raw.content[:20_000])}
    if len(origins) == 1:
        original = next(iter(origins))
        return original, domain == original
    return (domain or host.removeprefix("www."), domain is not None and not origins)


def _body_signature(doc: CleanDocument) -> tuple[str, set[tuple[str, ...]]]:
    words = tuple(re.findall(r"\w+", doc.raw.content[:30_000].casefold()))
    return hashlib.sha256(doc.raw.content.encode()).hexdigest(), {
        words[index:index + 5] for index in range(len(words) - 4)}


def _copied(left, right) -> bool:
    left_digest, left_words = left
    right_digest, right_words = right
    if left_digest == right_digest:
        return True
    return (min(len(left_words), len(right_words)) >= 8
            and len(left_words & right_words) / min(len(left_words), len(right_words)) >= .80)


def validate_events(docs: list[CleanDocument], cutoff: datetime, *,
                    security_ids: dict[str, str] | None = None) -> dict:
    """Recompute score eligibility from frozen source text, admission and annotations."""
    if cutoff.utcoffset() is None:
        raise ValueError("Event evidence requires a timezone-aware cutoff")
    stats = CrossValidationStats(total=len(docs))
    rows, groups = {}, defaultdict(list)
    for doc in docs:
        if doc.evidence is None:
            doc.evidence = evidence_for(doc.raw)
        doc.corroborated = False
        canon = _canonical_event(doc.events, str(doc.raw.meta.get("event_hint", "")))
        publication = _publication(doc.raw.published, cutoff)
        status = "semantic_annotation_missing" if doc.degraded else publication
        stats.llm_missing += int(doc.degraded)
        stats.missing_published_at += int(publication == "unknown_publication")
        stats.key_event_docs += int(canon is not None)
        issuer = _issuer_ir(doc.raw.url) or _filing_issuer(doc)
        origin, direct = _origin(doc, issuer)
        row = {"status": status, "origin": origin, "source_sha256": hashlib.sha256(doc.raw.content.encode()).hexdigest()}
        rows[doc.raw.doc_id] = row
        if status is not None:
            continue
        if canon is None:
            row["status"] = "eligible"
            doc.corroborated = True
            continue
        text = doc.raw.title + "\n" + doc.raw.content[:100_000]
        identity = _identity(doc, canon, text, cutoff, security_ids, issuer)
        if identity is None:
            row["status"] = "unresolved_event"
            continue
        row["event_id"] = "/".join(identity)
        groups[identity].append((doc, origin, direct, _body_signature(doc), row))
    for members in groups.values():
        # Keep actual publisher pages ahead of explicit wire copies. A wire
        # copy alone cannot fill an independent source slot.
        eligible = []
        for doc, origin, direct, signature, row in sorted(members, key=lambda item: not item[2]):
            duplicate = any(origin == key or _copied(signature, observed)
                            for _, key, observed, _ in eligible)
            if duplicate:
                row["status"] = "duplicate_origin"
            elif not direct:
                row["status"] = "unverified_publisher"
            else:
                eligible.append((doc, origin, signature, row))
        sufficient = len(eligible) >= 2 and any(
            SourceTier.of(doc.evidence.tier) <= SourceTier.T2 for doc, *_ in eligible)
        for doc, _, _, row in eligible:
            doc.corroborated = sufficient
            row["status"] = "corroborated" if sufficient else "insufficient_independent_sources"
            stats.corroborated += int(sufficient)
    stats.downgraded = sum(not doc.corroborated for doc in docs)
    return {**stats.to_dict(), "event_identity": {"contract": EVENT_CONTRACT, "documents": rows}}
