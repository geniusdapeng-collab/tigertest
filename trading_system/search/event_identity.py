"""Point-in-time corroboration for the required-model research profile."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .credibility import CrossValidationStats, SourceTier, _canonical_event, evidence_for
from .models import CleanDocument, StructuredDocument
from .ontology import (ANNOTATION_CONTRACT, CLAIM_KINDS, CLAIM_RELATIONS, EVENT_IDENTIFIER_SCHEMES,
                       EVIDENCE_USES, EVENT_TYPES,
                       IDENTIFIER_SCHEMES, PERIOD_KINDS, SUBJECT_TYPES,
                       TIME_PRECISIONS, normalized_name)

EVENT_CONTRACT = "point-in-time-event/v1"
STRUCTURED_EVENT_CONTRACT = "point-in-time-typed-event/v2"
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
                    security_ids: dict[str, str] | None = None,
                    contract: str = EVENT_CONTRACT, entity_registry=None,
                    source_policies=None) -> dict:
    """Recompute score eligibility from frozen source text, admission and annotations."""
    if contract == STRUCTURED_EVENT_CONTRACT or contract == "v2":
        return validate_structured_events(docs, cutoff, security_ids=security_ids,
                                          entity_registry=entity_registry,
                                          source_policies=source_policies)
    if contract != EVENT_CONTRACT:
        raise ValueError("Unknown event evidence contract")
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


# V2 is deliberately separate. None of these helpers changes the frozen v1
# event identity, request schemas or saved dataclass shapes.
_LOCAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _aware(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def _span_texts(ids, spans, *, required=True) -> list[str]:
    if (not isinstance(ids, list) or required and not ids or len(ids) != len(set(ids))
            or any(not isinstance(key, str) or key not in spans for key in ids)):
        raise ValueError("invalid_span_reference")
    return [spans[key]["quote"] for key in ids]


def _resolve_spans(doc: StructuredDocument) -> tuple[dict, dict]:
    """Bind exact quotes, optionally locating a unique quote deterministically."""
    if not isinstance(doc.evidence_spans, list) or len(doc.evidence_spans) > 128:
        raise ValueError("invalid_spans")
    body, result, errors = doc.raw.content, {}, {}
    for span in doc.evidence_spans:
        if not isinstance(span, dict) or set(span) != {"id", "start", "end", "quote"}:
            raise ValueError("invalid_span")
        key, quote = span["id"], span["quote"]
        if (not isinstance(key, str) or _LOCAL_ID.fullmatch(key) is None
                or not isinstance(quote, str) or not quote or len(quote) > 8000):
            raise ValueError("invalid_span")
        if key in result or key in errors:
            result.pop(key, None)
            errors[key] = "duplicate_span"
            continue
        start, end = span["start"], span["end"]
        if start is None and end is None:
            start = body.find(quote)
            if start < 0 or body.find(quote, start + 1) != -1:
                errors[key] = "ambiguous_or_missing_span"
                continue
            end = start + len(quote)
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(body) or body[start:end] != quote):
            errors[key] = "span_text_mismatch"
            continue
        result[key] = {"start": start, "end": end, "quote": quote,
                       "sha256": hashlib.sha256(quote.encode()).hexdigest()}
    return result, errors


def _literal(value: str, texts: list[str], *, identifier=False) -> bool:
    wanted = normalized_name(value)
    if not wanted:
        return False
    for text in texts:
        text = normalized_name(text)
        if identifier:
            # A sentence's terminal full stop is punctuation; a ticker suffix
            # such as .US/.B is still part of the identifier, not an alias we
            # may silently strip.
            if re.search(r"(?<![\w.\-])" + re.escape(wanted) + r"(?![\w\-]|\.[\w])", text):
                return True
        elif wanted in text:
            return True
    return False


def _registry_parts(registry, security_ids):
    securities = dict(security_ids or {})
    entities = {}
    if registry is not None:
        if not isinstance(registry, dict):
            raise ValueError("invalid_entity_registry")
        if "securities" in registry or "entities" in registry:
            supplied = registry.get("securities", {})
            entities = registry.get("entities", {})
        else:
            supplied = registry
        if (not isinstance(supplied, dict) or not isinstance(entities, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in supplied.items())):
            raise ValueError("invalid_entity_registry")
        if any(key in securities and securities[key] != value for key, value in supplied.items()):
            raise ValueError("conflicting_security_registry")
        securities.update(supplied)
    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in entities.items()):
        raise ValueError("invalid_entity_registry")
    return securities, entities


def _resolve_subject(subject, spans, securities, entities):
    expected = {"id", "type", "name", "identifiers", "span_ids"}
    if not isinstance(subject, dict) or set(subject) != expected:
        raise ValueError("invalid_subject")
    key, kind, name = subject["id"], subject["type"], subject["name"]
    if (not isinstance(key, str) or _LOCAL_ID.fullmatch(key) is None
            or kind not in SUBJECT_TYPES or not isinstance(name, str) or not name.strip()):
        raise ValueError("invalid_subject")
    if not _literal(name, _span_texts(subject["span_ids"], spans)):
        raise ValueError("subject_name_not_in_span")
    identifiers = subject["identifiers"]
    if not isinstance(identifiers, list) or len(identifiers) > 12:
        raise ValueError("invalid_subject_identifier")
    observed = []
    for item in identifiers:
        if (not isinstance(item, dict) or set(item) != {"scheme", "value", "span_ids"}
                or item["scheme"] not in IDENTIFIER_SCHEMES
                or not isinstance(item["value"], str)
                or not _literal(item["value"], _span_texts(item["span_ids"], spans), identifier=True)):
            raise ValueError("subject_identifier_not_in_span")
        observed.append((item["scheme"], item["value"]))
    named_entities = {
        entity_id for entity_id, record in entities.items() if record.get("type") == kind
        and normalized_name(name) in {normalized_name(alias)
            for alias in [record.get("name", ""), *record.get("aliases", [])]
            if isinstance(alias, str) and alias}
    }
    matches = []
    for entity_id, record in entities.items():
        if record.get("type") != kind:
            continue
        aliases = [record.get("name", ""), *record.get("aliases", [])]
        names_match = normalized_name(name) in {normalized_name(alias) for alias in aliases
                                               if isinstance(alias, str) and alias}
        registered = record.get("identifiers", {})
        found = []
        for scheme, value in observed:
            values = registered.get(scheme, []) if isinstance(registered, dict) else []
            values = [values] if isinstance(values, str) else values
            if any(normalized_name(value) == normalized_name(candidate) for candidate in values):
                found.append(scheme)
        if names_match or found:
            # A registered but contradictory ticker/CIK cannot be discarded in
            # favor of a convenient company name match.
            contradictions = [scheme for scheme, value in observed
                if scheme in {"ticker", "cik", "lei", "series"}
                and scheme in registered
                and normalized_name(value) not in {
                    normalized_name(candidate) for candidate in
                    ([registered[scheme]] if isinstance(registered[scheme], str) else registered[scheme])}]
            if not contradictions and (not named_entities or entity_id in named_entities):
                matches.append((entity_id, record))
    if len(matches) > 1:
        raise ValueError("ambiguous_subject")
    security_matches = {securities[value] for scheme, value in observed
                        if scheme == "ticker" and value in securities}
    if matches:
        entity_id, record = matches[0]
        bound = record.get("security_ids", [])
        if not isinstance(bound, list) or not all(isinstance(value, str) for value in bound):
            raise ValueError("invalid_entity_registry")
        bound = sorted(set(bound) & set(securities.values()))
        if security_matches and not security_matches <= set(bound):
            raise ValueError("subject_security_conflict")
        return {"id": entity_id, "type": kind, "name": name, "security_ids": bound,
                "registry_bound": True, "identifiers": observed,
                "registered_identifiers": record.get("identifiers", {}),
                "span_ids": sorted(set(subject["span_ids"]) | {
                    reference for item in identifiers for reference in item["span_ids"]})}
    if named_entities:
        raise ValueError("subject_identity_conflict")
    if len(security_matches) > 1:
        raise ValueError("ambiguous_subject_security")
    if security_matches and kind in {"issuer", "security"}:
        security = next(iter(security_matches))
        return {"id": security, "type": kind, "name": name, "security_ids": [security],
                "registry_bound": True, "identifiers": observed, "registered_identifiers": {},
                "span_ids": sorted(set(subject["span_ids"]) | {
                    reference for item in identifiers for reference in item["span_ids"]})}
    if kind == "unknown":
        raise ValueError("unresolved_subject")
    # A literally named overseas company/industry is a research subject. This
    # deterministic identity does not guess a ticker or expand the US roster.
    return {"id": "external:" + kind + ":" + hashlib.sha256(normalized_name(name).encode()).hexdigest()[:24],
            "type": kind, "name": name, "security_ids": [], "registry_bound": False,
            "identifiers": observed, "registered_identifiers": {},
            "span_ids": sorted(set(subject["span_ids"]) | {
                reference for item in identifiers for reference in item["span_ids"]})}


def _days_in(texts):
    result = set()
    for text in texts:
        for value in re.findall(_ISO_DAY + "|" + _LONG_DAY, text):
            day = _period_day(value)
            if day is not None:
                result.add(day)
        for year, month, day in re.findall(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text):
            try:
                result.add(date(int(year), int(month), int(day)))
            except ValueError:
                continue
    return result


def _period(record, spans):
    if not isinstance(record, dict) or set(record) != {"kind", "value", "span_ids"}:
        raise ValueError("invalid_period")
    kind, value = record["kind"], record["value"]
    if kind not in PERIOD_KINDS:
        raise ValueError("invalid_period")
    if kind == "unknown":
        if value is not None or record["span_ids"]:
            raise ValueError("unknown_period_has_value")
        return None
    texts = _span_texts(record["span_ids"], spans)
    if not isinstance(value, str):
        raise ValueError("invalid_period")
    if kind == "period_end":
        day = _period_day(value)
        if day is not None and day in _days_in(texts):
            return day.isoformat()
    elif kind == "quarter":
        periods = {f"{year or trailing_year}-Q{first or last}"
                   for text in texts for first, year, trailing_year, last in _FISCAL_QUARTER.findall(text)}
        for text in texts:
            for year, quarter in re.findall(r"(20\d{2})年\s*第?([一二三四1-4])季度", text):
                number = "一二三四".find(quarter) + 1 if quarter in "一二三四" else int(quarter)
                periods.add(f"{year}-Q{number}")
        if value in periods:
            return value
    elif kind == "fiscal_year":
        if re.fullmatch(r"20\d{2}", value) and any(re.search(
                r"\b(?:FY\s*|fiscal year\s+(?:ended\s+)?)" + value + r"\b", text, re.I)
                or re.search(value + r"年?财年", text) for text in texts):
            return value
    raise ValueError("period_not_in_span")


def _event_time(record, spans):
    if not isinstance(record, dict) or set(record) != {"value", "precision", "span_ids"}:
        raise ValueError("invalid_event_time")
    value, precision = record["value"], record["precision"]
    if precision not in TIME_PRECISIONS:
        raise ValueError("invalid_event_time")
    if precision == "unknown":
        if value is not None or record["span_ids"]:
            raise ValueError("unknown_time_has_value")
        return None
    texts = _span_texts(record["span_ids"], spans)
    if precision == "date":
        try:
            day = date.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValueError("invalid_event_time") from None
        if day not in _days_in(texts):
            raise ValueError("event_time_not_in_span")
        return day.isoformat()
    observed = _aware(value)
    if observed is not None:
        for text in texts:
            for candidate in re.findall(r"20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", text):
                parsed = _aware(candidate)
                if parsed is not None and parsed == observed:
                    return observed.isoformat()
    raise ValueError("event_time_not_in_span")


def _source_policy(doc, source_policies):
    if not isinstance(source_policies, dict):
        return None
    sources = source_policies.get("sources", {})
    source_id = doc.raw.meta.get("source_policy_id")
    policy = sources.get(source_id) if isinstance(sources, dict) else None
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return None
    if policy.get("source_id") != source_id:
        return None
    parsed = urlsplit(doc.raw.url)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.hostname not in policy.get("allowed_hosts", [])):
        return None
    return policy


def _authority(doc, policy):
    value = doc.raw.meta.get("authority")
    if not isinstance(policy, dict) or not isinstance(value, dict):
        return None
    if value.get("scope") not in policy.get("authority_scopes", []):
        return None
    proof = value.get("proof_ref")
    if (not isinstance(proof, dict) or proof.get("source_id") != policy["source_id"]
            or not isinstance(proof.get("response_sha256"), str)
            or not _SHA256.fullmatch(proof["response_sha256"])
            or proof.get("content_sha256") != hashlib.sha256(doc.raw.content.encode()).hexdigest()):
        return None
    observed = urlsplit(proof.get("source_url", ""))
    if (observed.scheme != "https" or observed.username is not None or observed.password is not None
            or observed.hostname not in policy.get("allowed_hosts", [])):
        return None
    if value["scope"] == "sec_filing":
        filing = _EDGAR_PATH.fullmatch(urlsplit(doc.raw.url).path)
        accession = proof.get("accession")
        issuer = str(value.get("issuer_id", "")).removeprefix("issuer:").removeprefix("cik:")
        if (value.get("proof_type") not in {"sec_submission_acceptance", "immutable_accession"}
                or filing is None or not isinstance(accession, str)
                or accession.replace("-", "") != filing[2]
                or _aware(proof.get("accepted_at")) is None or not issuer.isdigit()
                or int(issuer) != int(filing[1])):
            return None
    return value


def _version_known(doc, cutoff, authority):
    if authority is not None and authority.get("scope") == "sec_filing":
        if _aware(authority["proof_ref"].get("accepted_at")) > cutoff:
            return False
    observed = doc.raw.fetched_at
    if (type(observed) in {int, float} and math.isfinite(observed)
            and 0 < observed <= cutoff.timestamp()):
        return True
    if authority is None:
        return False
    proof = authority["proof_ref"]
    if authority.get("proof_type") == "archived_version":
        archived = _aware(proof.get("archive_first_seen_at"))
        return archived is not None and archived <= cutoff
    if (authority.get("scope") == "sec_filing" and authority.get("proof_type") in
            {"sec_submission_acceptance", "immutable_accession"}):
        accepted = _aware(proof.get("accepted_at"))
        parsed = _EDGAR_PATH.fullmatch(urlsplit(doc.raw.url).path)
        accession = proof.get("accession", "")
        return (accepted is not None and accepted <= cutoff and parsed is not None
                and isinstance(accession, str) and accession.replace("-", "") == parsed[2])
    return False


def _publication_status(doc, cutoff):
    times = doc.raw.meta.get("source_times", {})
    when = times.get("published", {}) if isinstance(times, dict) else {}
    value = when.get("value") if isinstance(when, dict) else None
    precision = when.get("precision") if isinstance(when, dict) else None
    if value is not None and precision not in {"instant", "date"}:
        return "unknown_publication", None
    if value is None:
        value = doc.raw.published
    if not isinstance(value, str) or not value:
        return "unknown_publication", None
    if re.fullmatch(_ISO_DAY, value) or precision == "date":
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            return "unknown_publication", None
        today = cutoff.astimezone(_MARKET_ZONE).date()
        return ("future_publication" if day > today else
                "unknown_publication" if day == today else None), day
    instant = _aware(value)
    if instant is None:
        try:
            instant = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return "unknown_publication", None
    if instant is None or instant.utcoffset() is None:
        return "unknown_publication", None
    return ("future_publication" if instant > cutoff else None), instant.astimezone(_MARKET_ZONE).date()


def _official_claim(record, subjects, authority):
    if authority is None:
        return False
    kind, scope = record["claim_kind"], authority["scope"]
    if scope in {"sec_filing", "issuer_ir"} and kind in {"issuer_disclosure", "forward_guidance"}:
        issuer = authority.get("issuer_id")
        identifiers = {value.lstrip("0") or "0" for subject in subjects
                       for scheme, value in subject["identifiers"] if scheme == "cik"}
        for subject in subjects:
            values = subject["registered_identifiers"].get("cik", [])
            for value in [values] if isinstance(values, str) else values:
                identifiers.add(value.lstrip("0") or "0")
        entities = {subject["id"] for subject in subjects}
        expected = (str(issuer).removeprefix("issuer:").removeprefix("cik:").lstrip("0") or "0")
        return (issuer is not None and all(subject["type"] in {"issuer", "security"} for subject in subjects)
                and (issuer in entities or expected in identifiers
                     or any(subject["id"] == "issuer:" + str(issuer) for subject in subjects)))
    if scope == "official_statistics" and kind == "official_statistic" and record["event_type"] == "macro":
        permitted = set(authority.get("series_ids", []))
        return bool(permitted) and all(subject["type"] == "macro_series" and subject["registry_bound"]
            and (subject["id"] in permitted or any(scheme == "series" and value in permitted
                  for scheme, value in subject["identifiers"])) for subject in subjects)
    return (scope == "official_policy" and kind == "official_policy"
            and record["event_type"] == "policy")


def _issuer_origin(authority, subjects):
    owner = str(authority.get("issuer_id", ""))
    value = owner.removeprefix("issuer:").removeprefix("cik:")
    if value.isdigit():
        return "issuer:cik:" + value.zfill(10)
    for subject in subjects:
        if subject["id"] == owner:
            values = subject["registered_identifiers"].get("cik", [])
            values = [values] if isinstance(values, str) else values
            if len(values) == 1 and values[0].isdigit():
                return "issuer:cik:" + values[0].zfill(10)
    return "issuer:" + owner.removeprefix("issuer:")


def _action_security_ids(authority, subjects, securities, entities):
    """Bind SEC action evidence only to its registered filing issuer.

    An issuer may discuss a customer, supplier or acquisition target in a filing.
    Their literal presence and research identities do not make this *their* SEC
    disclosure. The verified filing CIK must match a trusted registry owner,
    whose admitted securities are then intersected with the event subjects.
    Model-supplied ticker/CIK pairs cannot create the owner-to-security mapping.
    """
    if authority is None or authority.get("scope") != "sec_filing":
        return []
    owner = str(authority.get("issuer_id", "")).removeprefix("issuer:").removeprefix("cik:")
    if not owner.isdigit():
        return []
    owner = owner.lstrip("0") or "0"
    mentioned = {security for subject in subjects for security in subject["security_ids"]}
    admitted = set(securities.values())
    owned = set()
    for entity_id, record in entities.items():
        if record.get("type") not in {"issuer", "security"}:
            continue
        identifiers = record.get("identifiers", {})
        values = identifiers.get("cik", []) if isinstance(identifiers, dict) else []
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.isdigit() for value in values):
            continue
        ciks = {value.lstrip("0") or "0" for value in values}
        key = entity_id.removeprefix("issuer:").removeprefix("cik:")
        key_matches = key.isdigit() and (key.lstrip("0") or "0") == owner
        if (not key_matches and owner not in ciks) or ciks and ciks != {owner}:
            continue
        bound = record.get("security_ids", [])
        if not isinstance(bound, list) or any(not isinstance(value, str) for value in bound):
            continue
        owned.update(set(bound) & admitted & mentioned)
    return sorted(owned)


def _typed_identity(record, subjects, spans, authority):
    if not isinstance(record, dict) or set(record) != {
            "id", "event_type", "subject_ids", "is_primary", "event_identifiers", "report_period",
            "event_time", "effective_time", "source_span_ids", "claim_kind", "target_ids"}:
        raise ValueError("invalid_event_record")
    if (not isinstance(record["id"], str) or _LOCAL_ID.fullmatch(record["id"]) is None
            or record["event_type"] not in EVENT_TYPES or record["claim_kind"] not in CLAIM_KINDS
            or type(record["is_primary"]) is not bool):
        raise ValueError("invalid_event_record")
    _span_texts(record["source_span_ids"], spans)
    required_spans = {key for subject in subjects for key in subject["span_ids"]}
    for field in ("report_period", "event_time", "effective_time"):
        value = record[field]
        if not isinstance(value, dict) or not isinstance(value.get("span_ids"), list):
            raise ValueError("invalid_event_time_or_period")
        required_spans.update(value["span_ids"])
    if not required_spans <= set(record["source_span_ids"]):
        raise ValueError("event_evidence_scope_incomplete")
    targets = record["target_ids"]
    if (not isinstance(targets, list) or len(targets) != len(set(targets)) or len(targets) > 32
            or any(not isinstance(target, str) or _LOCAL_ID.fullmatch(target) is None for target in targets)):
        raise ValueError("invalid_event_target")
    period = _period(record["report_period"], spans)
    event_time, effective = (_event_time(record[name], spans) for name in ("event_time", "effective_time"))
    identifiers = record["event_identifiers"]
    if not isinstance(identifiers, list) or len(identifiers) > 12:
        raise ValueError("invalid_event_identifier")
    keys = []
    for item in identifiers:
        if (not isinstance(item, dict) or set(item) != {"scheme", "value", "span_ids"}
                or item["scheme"] not in EVENT_IDENTIFIER_SCHEMES or not isinstance(item["value"], str)
                or not _literal(item["value"], _span_texts(item["span_ids"], spans), identifier=True)):
            raise ValueError("event_identifier_not_in_span")
        keys.append(item["scheme"] + ":" + normalized_name(item["value"]))
        if not set(item["span_ids"]) <= set(record["source_span_ids"]):
            raise ValueError("event_evidence_scope_incomplete")
    entity = ",".join(sorted(subject["id"] for subject in subjects))
    event_type = record["event_type"]
    if event_type in {"other", "unknown"} or record["claim_kind"] in {"analysis", "rumor", "unknown"}:
        return None, period, event_time, effective
    if event_type in {"earnings", "guidance"} and period is not None:
        return (entity, event_type, period), period, event_time, effective
    if keys:
        return (entity, event_type, *sorted(set(keys))), period, event_time, effective
    if authority is not None and authority.get("scope") == "sec_filing":
        accession = authority["proof_ref"].get("accession")
        if isinstance(accession, str) and re.fullmatch(r"\d{10}-?\d{2}-?\d{6}", accession):
            return (entity, event_type, accession.replace("-", "")), period, event_time, effective
    if event_type == "macro" and period is not None:
        return (entity, event_type, period), period, event_time, effective
    when = effective or event_time
    # Two acquisitions/policies involving one company on the same day cannot
    # be grouped by date alone. Require an identified second subject or a
    # source identifier, rather than merging arbitrary model-generated titles.
    if when is not None and len(subjects) >= 2:
        return (entity, event_type, when), period, event_time, effective
    return None, period, event_time, effective


def _document_binding(doc):
    """Bind derived eligibility to both frozen source and semantic annotations."""
    value = {"source": doc.raw.source, "id": doc.raw.doc_id, "title": doc.raw.title,
             "url": doc.raw.url, "content": doc.raw.content, "published": doc.raw.published,
             "fetched_at": doc.raw.fetched_at, "meta": doc.raw.meta,
             "subjects": doc.subjects, "spans": doc.evidence_spans,
             "events": doc.event_records, "claims": doc.claims, "contract": doc.annotation_contract}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _claim_literal(value, texts):
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = normalized_name(value)
    if re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?", normalized):
        return any(re.search(r"(?<![\d.,])" + re.escape(normalized) + r"(?![\d,]|\.\d)",
                             normalized_name(text)) for text in texts)
    return _literal(value, texts, identifier=bool(re.search(r"\w", value)))


def _claims_for(doc, event_rows, spans):
    if not isinstance(doc.claims, list) or len(doc.claims) > 128:
        raise ValueError("invalid_claims")
    claims = {}
    for index, claim in enumerate(doc.claims):
        key = claim.get("id") if isinstance(claim, dict) else None
        if not isinstance(key, str) or _LOCAL_ID.fullmatch(key) is None:
            key = "invalid_claim_" + str(index)
        row = {"id": key, "claim_id": doc.raw.doc_id + ":" + key,
               "status": "unverified_claim", "allowed_uses": ["background"],
               "target_ids": [], "source_span_ids": [], "conflict_ids": [],
               "action_security_ids": []}
        if key in claims:
            claims[key].update(status="unverified_claim", reason="duplicate_claim",
                                allowed_uses=["background"])
            row["reason"] = "duplicate_claim"
            claims["invalid_claim_" + str(index)] = row
            continue
        claims[key] = row
        try:
            if not isinstance(claim, dict) or set(claim) != {
                    "id", "event_id", "predicate", "value", "unit", "currency", "source_span_ids",
                    "relation", "conflict_group", "supersedes_claim_id"}:
                raise ValueError("invalid_claim")
            event = event_rows.get(claim["event_id"])
            if event is None or event["status"] == "unverified_event":
                raise ValueError("claim_event_unverified")
            if (not isinstance(claim["predicate"], str) or not claim["predicate"].strip()
                    or claim["relation"] not in CLAIM_RELATIONS
                    or any(value is not None and (not isinstance(value, str) or not value.strip())
                           for value in (claim["conflict_group"], claim["supersedes_claim_id"]))):
                raise ValueError("invalid_claim")
            texts = _span_texts(claim["source_span_ids"], spans)
            if not set(claim["source_span_ids"]) <= set(event["source_span_ids"]):
                raise ValueError("claim_evidence_scope_incomplete")
            for field in ("value", "unit", "currency"):
                if not _claim_literal(claim[field], texts):
                    raise ValueError("claim_literal_not_in_span")
            row.update(event_id=claim["event_id"], event_identity=event.get("event_id"),
                       predicate=claim["predicate"], value=claim["value"], unit=claim["unit"],
                       currency=claim["currency"], report_period=event["report_period"],
                       relation=claim["relation"], conflict_group=claim["conflict_group"],
                       supersedes_claim_id=claim["supersedes_claim_id"],
                       supersession_status="unverified" if claim["supersedes_claim_id"] else "not_declared",
                       subject_ids=event["subject_ids"], target_ids=event["target_ids"],
                       tradable_security_ids=event["tradable_security_ids"],
                       source_span_ids=list(claim["source_span_ids"]),
                       status="span_verified", allowed_uses=["background", "analysis"])
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            reason = str(error) if isinstance(error, ValueError) else "invalid_claim"
            row["reason"] = reason if re.fullmatch(r"[a-z_]+", reason) else "invalid_claim"
    return claims


def _mark_claim_conflicts(docs):
    groups = defaultdict(list)
    for doc in docs:
        for claim in doc.eligibility["claims"]:
            if "scoring" not in claim["allowed_uses"]:
                continue
            key = (tuple(sorted(claim["subject_ids"])), claim["event_identity"],
                   normalized_name(claim["predicate"]),
                   normalized_name(claim["unit"] or ""), normalized_name(claim["currency"] or ""))
            groups[key].append(claim)
    for key, claims in groups.items():
        values = {normalized_name(claim["value"]) for claim in claims if claim["value"] is not None}
        contrary = {claim["relation"] for claim in claims} >= {"supports", "refutes"}
        if len(values) <= 1 and not contrary:
            continue
        conflict = "conflict_" + hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]
        for claim in claims:
            # Conflicting primary facts remain available as evidenced risk or
            # counterevidence; their target cannot receive a resolved score.
            claim["status"] = "conflicted"
            claim["conflict_ids"] = [conflict]


def validate_structured_events(docs, cutoff: datetime, *, security_ids=None,
                               entity_registry=None, source_policies=None) -> dict:
    """Validate typed, span-bound research events without expanding trade scope.

    Original authenticity, version availability, independent corroboration and
    per-target use are distinct. Unknown/non-factual annotations never receive
    score permission merely because another article was found.
    """
    if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
        raise ValueError("Event evidence requires a timezone-aware cutoff")
    securities, entities = _registry_parts(entity_registry, security_ids)
    if len({doc.raw.doc_id for doc in docs}) != len(docs):
        raise ValueError("Duplicate structured document identity")
    stats = CrossValidationStats(total=len(docs))
    documents, groups = {}, defaultdict(list)
    resolved_event_subjects = {}
    for doc in docs:
        if not isinstance(doc, StructuredDocument) or doc.annotation_contract != ANNOTATION_CONTRACT:
            raise ValueError("Structured evidence contract required")
        doc.corroborated = False
        doc.evidence = evidence_for(doc.raw)
        body_hash = hashlib.sha256(doc.raw.content.encode()).hexdigest()
        document = {"status": "background", "source_sha256": body_hash, "events": {}, "claims": {}}
        documents[doc.raw.doc_id] = document
        doc.eligibility = {"contract": STRUCTURED_EVENT_CONTRACT, "cutoff": cutoff.isoformat(),
                           "source_sha256": body_hash, "document_binding_sha256": _document_binding(doc),
                           "events": [], "claims": [], "resolved_spans": {}}
        publication, published_day = _publication_status(doc, cutoff)
        stats.missing_published_at += int(publication == "unknown_publication")
        stats.llm_missing += int(doc.degraded)
        if doc.degraded:
            document["status"] = "semantic_annotation_missing"
            continue
        try:
            spans, span_errors = _resolve_spans(doc)
            if not isinstance(doc.subjects, list) or len(doc.subjects) > 64:
                raise ValueError("invalid_subjects")
            resolved, subject_errors = {}, {}
            for subject in doc.subjects:
                if (not isinstance(subject, dict) or not isinstance(subject.get("id"), str)
                        or _LOCAL_ID.fullmatch(subject["id"]) is None):
                    raise ValueError("invalid_subject")
                subject_key = subject["id"]
                if subject_key in resolved or subject_key in subject_errors:
                    resolved.pop(subject_key, None)
                    subject_errors[subject_key] = "duplicate_subject"
                    continue
                try:
                    resolved[subject_key] = _resolve_subject(subject, spans, securities, entities)
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    reason = str(error) if isinstance(error, ValueError) else "invalid_subject"
                    subject_errors[subject_key] = (reason if re.fullmatch(r"[a-z_]+", reason)
                                                   else "invalid_subject")
            if not isinstance(doc.event_records, list) or len(doc.event_records) > 64:
                raise ValueError("invalid_event_records")
        except (ValueError, TypeError, KeyError, AttributeError):
            document["status"] = "invalid_annotation"
            continue
        doc.eligibility["resolved_spans"] = spans
        document["span_errors"] = span_errors
        document["subject_errors"] = subject_errors
        policy = _source_policy(doc, source_policies)
        authority = _authority(doc, policy)
        version_known = _version_known(doc, cutoff, authority)
        complete = (doc.raw.meta.get("completeness") == "complete"
                    and doc.raw.meta.get("text_kind") in {"html_main", "pdf_text", "data", "provider_markdown"})
        for index, record in enumerate(doc.event_records):
            row = {"status": "unresolved_event", "allowed_uses": ["background"],
                   "source_span_ids": [], "target_ids": [], "subject_ids": [],
                   "tradable_security_ids": [], "action_security_ids": [], "source_sha256": body_hash}
            try:
                key = record["id"]
                if key in document["events"]:
                    document["events"][key].update(status="unverified_event", reason="duplicate_event",
                                                    allowed_uses=["background"])
                    raise ValueError("duplicate_event")
                subject_keys = record["subject_ids"]
                if (not isinstance(subject_keys, list) or not subject_keys
                        or len(subject_keys) != len(set(subject_keys))
                        or any(subject not in resolved for subject in subject_keys)):
                    raise ValueError("unresolved_subject")
                subjects = [resolved[subject] for subject in subject_keys]
                identity, period, event_time, effective = _typed_identity(record, subjects, spans, authority)
                row.update(id=key, target_ids=list(record["target_ids"]),
                           subject_ids=[subject["id"] for subject in subjects],
                           tradable_security_ids=sorted({value for subject in subjects for value in subject["security_ids"]}),
                           source_span_ids=list(record["source_span_ids"]), event_type=record["event_type"],
                           claim_kind=record["claim_kind"], is_primary=record["is_primary"],
                           report_period=period, event_time=event_time, effective_time=effective,
                           allowed_uses=["background", "analysis"])
                resolved_event_subjects[(doc.raw.doc_id, key)] = subjects
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                reason = str(error) if isinstance(error, ValueError) else "invalid_event_record"
                reason = reason if re.fullmatch(r"[a-z_]+", reason) else "invalid_event_record"
                key = record.get("id") if isinstance(record, dict) else None
                if not isinstance(key, str) or _LOCAL_ID.fullmatch(key) is None or key in document["events"]:
                    key = "invalid_event_" + str(index)
                row.update(id=key, status="unverified_event", reason=reason)
                document["events"][key] = row
                continue
            document["events"][key] = row
            stats.key_event_docs += int(record["event_type"] not in {"other", "unknown"})
            origin, direct = _origin(doc, None)
            official = _official_claim(record, subjects, authority)
            if official:
                owner = authority.get("issuer_id")
                origin = (_issuer_origin(authority, subjects) if owner is not None else
                          "official:" + authority["scope"] + ":" + (urlsplit(doc.raw.url).hostname or ""))
            row["origin"] = origin
            if publication is not None:
                row["status"] = publication
            elif not version_known:
                row["status"] = "version_unverified_at_cutoff"
            elif not complete:
                row["status"] = "original_body_unverified"
            elif not record["is_primary"]:
                row["status"] = "context_event"
            elif identity is None:
                row["status"] = "background_claim" if record["event_type"] in {"other", "unknown"} or record["claim_kind"] in {"analysis", "rumor", "unknown"} else "unresolved_event"
            elif (record["event_type"] == "earnings" and period is not None
                  and (re.fullmatch(_ISO_DAY, period) and date.fromisoformat(period) > cutoff.astimezone(_MARKET_ZONE).date()
                       or re.match(r"20\d{2}", period) and int(period[:4]) > cutoff.year)):
                row["status"] = "future_report_period"
            elif published_day is not None and cutoff.astimezone(_MARKET_ZONE).date() - published_day > _EVENT_MAX_AGE:
                # Preserve older financial originals as explicit analytical
                # baselines; they do not prove a current catalyst or buy news.
                row["status"] = "historical_baseline" if official and record["event_type"] in {"earnings", "guidance"} else "stale_publication"
            else:
                row["event_id"] = "/".join(identity)
                row["status"] = "pending_corroboration"
                groups[identity].append((doc, row, origin, direct or official,
                                         official, authority, _body_signature(doc)))
        doc.eligibility["events"] = list(document["events"].values())
        try:
            document["claims"] = _claims_for(doc, document["events"], spans)
        except ValueError:
            document["status"] = "invalid_annotation"
            for row in doc.eligibility["events"]:
                row.update(status="unverified_event", reason="invalid_claims", allowed_uses=["background"])
            continue
        doc.eligibility["claims"] = list(document["claims"].values())
        for row in doc.eligibility["events"]:
            supported = [claim for claim in doc.eligibility["claims"]
                         if claim.get("event_id") == row["id"] and claim["status"] == "span_verified"
                         and claim["relation"] in {"supports", "refutes"}]
            row["claim_ids"] = [claim["claim_id"] for claim in supported]
            if row["status"] == "pending_corroboration" and not supported:
                row["status"] = "no_verified_claim"
        main_periods = defaultdict(set)
        for row in doc.eligibility["events"]:
            if row.get("is_primary") and row.get("event_type") in {"earnings", "guidance"}:
                main_periods[(tuple(sorted(row["subject_ids"])), row["event_type"])].add(row["report_period"])
        for row in doc.eligibility["events"]:
            if (row.get("is_primary") and row.get("event_type") in {"earnings", "guidance"}
                    and len(main_periods[(tuple(sorted(row["subject_ids"])), row["event_type"])]) > 1):
                row["status"] = "ambiguous_primary_period"
    for members in groups.values():
        # An invalid record must not leave a formerly queued sibling eligible.
        members = [item for item in members if item[1]["status"] == "pending_corroboration"]
        originals, official_versions = [], []
        for item in sorted(members, key=lambda value: (not value[4],
                not (value[5] is not None and value[5].get("scope") == "sec_filing"), not value[3])):
            doc, row, origin, direct, official, authority, signature = item
            duplicate = any(origin == old[2] or _copied(signature, old[6]) for old in originals)
            if official:
                # Verified revisions/SEC+IR remain source facts even when they
                # share one issuer origin. Do not hide a correction/conflict,
                # and do not pretend it supplies another independent witness.
                official_versions.append(item)
                row["origin_duplicate"] = duplicate
                if not duplicate:
                    originals.append(item)
            elif duplicate:
                row["status"] = "duplicate_origin"
            elif not direct:
                row["status"] = "unverified_publisher"
            else:
                originals.append(item)
        sufficient = len(originals) >= 2
        qualified = official_versions + [item for item in originals if not item[4]]
        for doc, row, origin, direct, official, authority, signature in qualified:
            row["independent_origin_count"] = len(originals)
            if official or sufficient:
                row["status"] = "verified_original" if official else "corroborated"
                row["allowed_uses"].append("scoring")
                if (official and authority["scope"] == "sec_filing"
                        and row["tradable_security_ids"] and row["claim_kind"] == "issuer_disclosure"):
                    subjects = resolved_event_subjects[(doc.raw.doc_id, row["id"])]
                    row["action_security_ids"] = _action_security_ids(authority, subjects, securities, entities)
                    if row["action_security_ids"]:
                        row["allowed_uses"].append("action")
            else:
                row["status"] = "insufficient_independent_sources"
    for doc in docs:
        events = {row["id"]: row for row in doc.eligibility["events"]}
        for claim in doc.eligibility["claims"]:
            event = events.get(claim.get("event_id"))
            if (claim["status"] == "span_verified" and event is not None
                    and "scoring" in event["allowed_uses"] and claim["relation"] in {"supports", "refutes"}):
                claim.update(status="verified", event_identity=event["event_id"],
                             allowed_uses=list(event["allowed_uses"]),
                             action_security_ids=list(event["action_security_ids"]))
    _mark_claim_conflicts(docs)
    for doc in docs:
        document = documents[doc.raw.doc_id]
        eligible = [row for row in doc.eligibility["claims"] if "scoring" in row["allowed_uses"]]
        doc.corroborated = bool(eligible)
        if eligible:
            document["status"] = ("conflicted" if any(row["status"] == "conflicted" for row in eligible)
                                  else "eligible")
        elif document["status"] != "invalid_annotation" and document["events"]:
            document["status"] = next(iter(document["events"].values()))["status"]
        stats.corroborated += int(doc.corroborated)
    stats.downgraded = sum(not doc.corroborated for doc in docs)
    return {**stats.to_dict(), "scorable_events": sum(
        "scoring" in row["allowed_uses"] for doc in docs for row in doc.eligibility["events"]),
        "event_identity": {"contract": STRUCTURED_EVENT_CONTRACT, "documents": documents}}


def target_evidence_sources(docs, target_ids, *, use="scoring") -> dict[str, list[str]]:
    """Return per-target source ids only from current computed eligibility."""
    if use not in EVIDENCE_USES or len(target_ids) != len(set(target_ids)):
        raise ValueError("Invalid target evidence request")
    result = {target: [] for target in target_ids}
    for doc in docs:
        if not isinstance(doc, StructuredDocument):
            continue
        eligibility = doc.eligibility
        if (eligibility.get("contract") != STRUCTURED_EVENT_CONTRACT
                or eligibility.get("source_sha256") != hashlib.sha256(doc.raw.content.encode()).hexdigest()
                or eligibility.get("document_binding_sha256") != _document_binding(doc)):
            continue
        targets = {target for row in eligibility.get("claims", []) if use in row.get("allowed_uses", [])
                   for target in row.get("target_ids", [])}
        for target in result.keys() & targets:
            if doc.raw.doc_id not in result[target]:
                result[target].append(doc.raw.doc_id)
    return result


def target_evidence_coverage(docs, target_ids, *, use="scoring") -> dict:
    """Availability/conflicts are separate from the later model assessment."""
    sources = target_evidence_sources(docs, target_ids, use=use)
    result = {target: {"status": "available" if owned else "insufficient_evidence",
                       "source_ids": list(owned), "claim_ids": [], "conflict_ids": []}
              for target, owned in sources.items()}
    for doc in docs:
        if not isinstance(doc, StructuredDocument):
            continue
        for target, cell in result.items():
            if doc.raw.doc_id not in sources[target]:
                continue
            for claim in doc.eligibility.get("claims", []):
                if target in claim.get("target_ids", []) and use in claim.get("allowed_uses", []):
                    cell["claim_ids"].append(claim["claim_id"])
                    cell["conflict_ids"].extend(claim.get("conflict_ids", []))
    for cell in result.values():
        cell["claim_ids"] = sorted(set(cell["claim_ids"]))
        cell["conflict_ids"] = sorted(set(cell["conflict_ids"]))
        if cell["conflict_ids"]:
            cell["status"] = "conflicted"
    return result
