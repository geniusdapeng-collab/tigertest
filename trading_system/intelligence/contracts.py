"""Versioned, credential-free acquisition contracts and deterministic utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import math
import re
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


CONTRACT = "tiger.intelligence/v1"
DOCUMENT_SCHEMA = "tiger.intelligence-document/v1"
BUNDLE_SCHEMA = "tiger.evidence-bundle/v1"
PLAN_SCHEMA = "tiger.intelligence-plan/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,255}$")
_SECRET_KEY = re.compile(r"(?i)^(?:authorization|api[_-]?key|access[_-]?token|password|secret|credential|x-api-key|x-subscription-token)$")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_URL_TEXT = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_CREDENTIAL_QUERY = {"auth", "authorization", "token", "key", "apikey", "accesskey", "accesskeyid",
    "accesstoken", "refreshtoken", "idtoken", "password", "passwd", "secret", "signature", "credential",
    "credentials", "session", "sessionid", "sessiontoken", "clientsecret", "securitytoken", "jwt"}


def credential_free_url(url: str, *, endpoint=False, _depth=0) -> bool:
    try:
        if _depth > 4:
            return False
        decoded = html.unescape(url)
        parts = urlsplit(decoded)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
                or parts.password is not None or "\\" in url or endpoint and parts.fragment
                or any(ord(c) < 33 for c in url)):
            return False
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            for _ in range(8):
                expanded = unquote(key)
                if expanded == key:
                    break
                key = expanded
            if unquote(key) != key:
                return False
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _CREDENTIAL_QUERY or normalized.startswith(("xamz", "xgoog")):
                return False
            for _ in range(8):
                expanded = unquote(value)
                if expanded == value:
                    break
                value = expanded
            if value.startswith(("https://", "http://")) and not credential_free_url(value, _depth=_depth + 1):
                return False
        return True
    except (ValueError, UnicodeError):
        return False


class IntelligenceError(RuntimeError):
    """A public diagnostic never contains transport exceptions or credentials."""

    def __init__(self, code: str, message: str = "", *, details: dict | None = None):
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", code) is None:
            raise ValueError("Invalid intelligence diagnostic code")
        self.code = code
        self.details = safe_value(details or {})
        super().__init__(message or code)

    def to_dict(self) -> dict:
        return {"code": self.code, "details": self.details}


class SourceError(IntelligenceError):
    def __init__(self, code: str, *, http_status: int | None = None,
                 retry_after: float | None = None, delivery: str = "unknown",
                 bytes_received: int = 0, details: dict | None = None):
        self.http_status = http_status
        self.retry_after = retry_after
        self.delivery = delivery
        self.bytes_received = bytes_received
        super().__init__(code, details={**(details or {}), "http_status": http_status,
            "retry_after": retry_after, "delivery": delivery, "bytes_received": bytes_received})


def canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise IntelligenceError("CONTRACT_INVALID", "Non-canonical JSON value") from None


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value: object, label: str = "identity") -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise IntelligenceError("CONTRACT_INVALID", "Invalid " + label)
    return value


def _safe_text_url(match):
    value = match[0]
    candidate = value
    if value.endswith("\\") and match.string[match.end():match.end() + 1] in {"\"", "'"}:
        candidate = value.rstrip("\\")
    decoded = re.sub(r"\\+u([0-9a-fA-F]{4})", lambda item: chr(int(item[1], 16)), candidate)
    decoded = html.unescape(decoded)
    # Inspect the complete decoded token before choosing its display boundary.
    # Removing whitespace for this check cannot hide a later credential key.
    compact = re.sub(r"\s+", "", decoded)
    if len(compact) > 8192 or not credential_free_url(compact):
        return "[REDACTED_URL]"
    starts = list(re.finditer(r"https?://", compact, re.I))
    if len(starts) > 32:
        return "[REDACTED_URL]"
    for start in starts:
        # Overlapping starts matter: a second URL can be embedded in a path,
        # or introduced by encoded markup inside the same raw text token.
        nested = re.split(r"[<>\"'\x60]", compact[start.start():], maxsplit=1)[0]
        if not credential_free_url(nested):
            return "[REDACTED_URL]"
    # Only after every credential check may decoded HTML end a public URL.
    public = re.split(r"[\s<>\"'\x60]", decoded, maxsplit=1)[0]
    return value if credential_free_url(public) else "[REDACTED_URL]"



def safe_value(value: object, secrets: tuple[str, ...] = (), depth: int = 0):
    """Sanitize persisted metadata; raw content hashes describe the original bytes."""
    if depth > 48:
        raise IntelligenceError("CONTRACT_INVALID", "Metadata nesting exceeds contract")
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED_SECRET]")
        value = _JWT.sub("[REDACTED_TOKEN]", value)
        return _URL_TEXT.sub(_safe_text_url, value)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            original_key = str(key)
            safe_key = safe_value(original_key, secrets, depth + 1)
            if safe_key in result:
                raise IntelligenceError("CONTRACT_INVALID", "Metadata keys collide after sanitization")
            result[safe_key] = "[REDACTED_SECRET]" if _SECRET_KEY.fullmatch(original_key) else safe_value(child, secrets, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [safe_value(v, secrets, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise IntelligenceError("CONTRACT_INVALID", "Metadata must be finite JSON")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: str | datetime, *, code: str = "BAD_AS_OF") -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise IntelligenceError(code, "Timezone-aware timestamp required") from None


def timestamp(value: str | datetime) -> str:
    return aware(value).isoformat()


def source_time(raw: object, *, evidence: dict | None = None) -> dict:
    """Retain date-only and unknown values instead of inventing midnight precision."""
    result = {"value": None, "precision": "unknown", "timezone": None,
              "raw": raw if isinstance(raw, str) else None, "evidence": evidence or {}}
    if not isinstance(raw, str) or not raw.strip():
        return result
    text = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            result.update(value=date.fromisoformat(text).isoformat(), precision="date")
        except ValueError:
            return result
        return result
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (ValueError, TypeError, OverflowError):
            return result
    if parsed is not None and parsed.utcoffset() is not None:
        result.update(value=parsed.astimezone(timezone.utc).isoformat(),
                      precision="instant", timezone=str(parsed.tzinfo))
    return result


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (not credential_free_url(url) or parts.scheme not in {"https", "http"} or not parts.hostname or parts.username
                or parts.password or len(url) > 8192 or any(ord(x) < 33 for x in url)):
            raise ValueError
        params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                           urlencode(params, doseq=True), ""))
    except ValueError:
        raise IntelligenceError("CONTRACT_INVALID", "Invalid document URL") from None


@dataclass(frozen=True)
class SearchRequest:
    query: str
    count: int = 10
    domains: tuple[str, ...] = ()
    language: str = "en"
    start_date: str | None = None
    end_date: str | None = None
    topic: str = "general"
    entities: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    page: int = 0
    context: dict = field(default_factory=dict)

    def __post_init__(self):
        if (not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 10000
                or type(self.count) is not int or not 1 <= self.count <= 100
                or type(self.page) is not int or self.page < 0):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid search request")
        for value in (self.start_date, self.end_date):
            if value is not None:
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    raise IntelligenceError("CONTRACT_INVALID", "Invalid search date") from None
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise IntelligenceError("CONTRACT_INVALID", "Inverted search interval")
        canonical(self.context)

    def to_dict(self) -> dict:
        return {"query": self.query, "count": self.count, "domains": list(self.domains),
                "language": self.language, "start_date": self.start_date,
                "end_date": self.end_date, "topic": self.topic,
                "entities": list(self.entities), "targets": list(self.targets),
                "page": self.page, "context": safe_value(self.context)}


@dataclass
class SearchResult:
    hits: list[dict]
    request: dict
    response_metadata: dict = field(default_factory=dict)
    usage: dict = field(default_factory=lambda: {"state": "unknown", "requires_reconciliation": True})
    warnings: list[str] = field(default_factory=list)
    raw_bytes: bytes = b""

    def to_dict(self) -> dict:
        return safe_value({"hits": self.hits, "request": self.request,
            "response_metadata": self.response_metadata, "usage": self.usage, "warnings": self.warnings})


@dataclass
class FetchResult:
    url: str
    final_url: str
    title: str
    text: str
    raw_bytes: bytes
    media_type: str
    text_kind: str
    completeness: str
    published: dict = field(default_factory=lambda: source_time(None))
    updated: dict = field(default_factory=lambda: source_time(None))
    metadata: dict = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"state": "unknown", "requires_reconciliation": True})


Clock = Callable[[], datetime]
