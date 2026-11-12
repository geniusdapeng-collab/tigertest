"""Conservative robots admission over the caller's metered HTTP transport.

RFC 9309 defines matching; Tiger deliberately blocks empty/unclear files and
4xx, and imposes a 64 KiB acquisition budget. This is stricter than RFC defaults
and is not a claim of full RFC conformance (whose parser floor is 500 KiB).
Robots is an access preference, never a substitute for a source license.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit
import uuid

from .contracts import SourceError, canonical, credential_free_url, digest, identifier
from .transport import retry_after_seconds


USER_AGENT = "WorkLoom-Tiger-Research"
MAX_ROBOTS_BYTES = 65536
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_FIELDS = frozenset({"schema", "tenant", "origin", "policy_sha256", "user_agent", "observed_at",
    "expires_at", "retry_at", "result", "reason", "http_status", "body_available",
    "body_sha256", "body_bytes", "bytes_received", "request_count"})


def _blocked(reason, *, retry_after=None, http_status=None, bytes_received=0, request_count=0):
    return SourceError("FETCH_DENIED", delivery="not_sent", retry_after=retry_after,
        http_status=http_status, bytes_received=bytes_received,
        details={"reason": reason, "policy": "robots", "request_count": request_count})


def _invalid_constant(_value):
    raise ValueError("Non-finite robots observation")


def _origin(url):
    try:
        if not isinstance(url, str) or len(url) > 8192 or not credential_free_url(url):
            raise ValueError
        parts = urlsplit(url)
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if not host or parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        authority = "[" + host + "]" if ":" in host else host
        port = parts.port
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            authority += ":" + str(port)
        return urlunsplit((parts.scheme.lower(), authority, "", "", "")), host, parts
    except (ValueError, AttributeError, UnicodeError):
        raise _blocked("ROBOTS_URL_INVALID") from None


def _normalized_path(value, *, pattern=False):
    """Compare URI octets without decoding reserved percent-escaped octets."""
    result = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            if index + 2 >= len(value) or re.fullmatch(r"[0-9a-fA-F]{2}", value[index + 1:index + 3]) is None:
                raise _blocked("ROBOTS_SYNTAX_INVALID")
            octet = int(value[index + 1:index + 3], 16)
            result.append(chr(octet) if octet in _UNRESERVED else f"%{octet:02X}")
            index += 3
            continue
        if ord(char) <= 32 or ord(char) == 127:
            raise _blocked("ROBOTS_SYNTAX_INVALID")
        if ord(char) > 127 or not pattern and char in {"*", "$"}:
            result.extend(f"%{octet:02X}" for octet in char.encode("utf-8"))
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _matches(pattern, path):
    """Wildcard matching without a backtracking regular expression."""
    anchored = pattern.endswith("$")
    pieces = (pattern[:-1] if anchored else pattern).split("*")
    if len(pieces) == 1:
        return path == pieces[0] if anchored else path.startswith(pieces[0])
    if not path.startswith(pieces[0]):
        return False
    offset = len(pieces[0])
    for piece in pieces[1:-1]:
        found = path.find(piece, offset)
        if found < 0:
            return False
        offset = found + len(piece)
    last = pieces[-1]
    if anchored:
        return path.endswith(last) and len(path) - len(last) >= offset
    return path.find(last, offset) >= 0


@dataclass(frozen=True)
class _Rule:
    allow: bool
    pattern: str
    specificity: int


def _rules(body):
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise _blocked("ROBOTS_ENCODING_INVALID") from None
    if any(ord(char) < 32 and char not in "\t\r\n" or ord(char) == 127 for char in text):
        raise _blocked("ROBOTS_SYNTAX_INVALID")
    groups = []
    agents, rules = [], []
    seen_directive = False
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip(" \t")
        if not value:
            continue
        if len(value) > 8192 or ":" not in value:
            raise _blocked("ROBOTS_SYNTAX_INVALID")
        key, value = (part.strip(" \t") for part in value.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if re.fullmatch(r"(?:[A-Za-z_-]+|\*)", value) is None:
                raise _blocked("ROBOTS_SYNTAX_INVALID")
            if seen_directive:
                groups.append((tuple(agents), tuple(rules)))
                agents, rules, seen_directive = [], [], False
            agents.append(value.lower())
        elif key in {"allow", "disallow"}:
            if not agents:
                raise _blocked("ROBOTS_SYNTAX_INVALID")
            seen_directive = True
            if not value:
                # An explicit empty line is not sufficient affirmative policy
                # under Tiger's stricter no-empty-rule admission requirement.
                continue
            if value[0] not in {"/", "*"} or "$" in value[:-1]:
                raise _blocked("ROBOTS_SYNTAX_INVALID")
            pattern = _normalized_path(value, pattern=True)
            literal = pattern[:-1] if pattern.endswith("$") else pattern
            specificity = len(unquote_to_bytes(literal.replace("*", "")))
            rules.append(_Rule(key == "allow", pattern, specificity))
        elif key == "sitemap":
            # Other records do not terminate a group; never follow a URL in
            # robots or turn its text into model instructions.
            if not value:
                raise _blocked("ROBOTS_SYNTAX_INVALID")
        else:
            # Unknown/misspelled or rate extensions cannot silently weaken
            # policy. Host-level delays are independently metered by Tiger.
            raise _blocked("ROBOTS_UNSUPPORTED_DIRECTIVE")
    if agents:
        groups.append((tuple(agents), tuple(rules)))
    exact = [group for group in groups if USER_AGENT.lower() in group[0]]
    selected = exact or [group for group in groups if "*" in group[0]]
    combined = tuple(rule for _, group_rules in selected for rule in group_rules)
    if not combined:
        raise _blocked("ROBOTS_NO_APPLICABLE_RULES")
    return combined


class RobotsPolicy:
    def __init__(self, root, tenant="public", *, cache_ttl_seconds=3600.0,
                 failure_ttl_seconds=60.0, public_api_exemptions=(), clock=None):
        self.tenant = identifier(tenant, "tenant")
        for value in (cache_ttl_seconds, failure_ttl_seconds):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= 86400):
                raise _blocked("ROBOTS_CONFIG_INVALID")
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self.failure_ttl_seconds = float(failure_ttl_seconds)
        self.clock = clock or time.time
        self.exemptions = []
        for entry in public_api_exemptions:
            if (not isinstance(entry, dict) or set(entry) != {"origin", "exact_path", "contract_sha256", "method"}
                    or entry["method"] != "GET"
                    or not isinstance(entry["exact_path"], str) or not entry["exact_path"].startswith("/")
                    or any(char in entry["exact_path"] for char in "?#*")
                    or re.fullmatch(r"[0-9a-f]{64}", str(entry["contract_sha256"])) is None):
                raise _blocked("ROBOTS_CONFIG_INVALID")
            origin, _, parts = _origin(entry["origin"])
            if parts.path or parts.query or parts.fragment:
                raise _blocked("ROBOTS_CONFIG_INVALID")
            _normalized_path(entry["exact_path"])
            value = {**entry, "origin": origin}
            if value in self.exemptions:
                raise _blocked("ROBOTS_CONFIG_INVALID")
            self.exemptions.append(value)
        self.exemptions = tuple(self.exemptions)
        self.policy_sha256 = digest({"schema": "tiger.robots-policy/v1", "user_agent": USER_AGENT,
            "max_bytes": MAX_ROBOTS_BYTES, "cache_ttl_seconds": self.cache_ttl_seconds,
            "failure_ttl_seconds": self.failure_ttl_seconds, "exemptions": self.exemptions,
            "duplicate_media_type_policy": "text_plain_present_only_known_text_labels_and_strict_rules"})
        self.root = Path(root).absolute()
        self.directory = self.root / "robots-policy"
        self.path = self.directory / "observations.sqlite3"
        try:
            self._safe_paths()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._safe_paths()
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                    raise _blocked("ROBOTS_CACHE_UNSAFE")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            with self._connection() as db:
                db.execute("CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,record TEXT NOT NULL,body BLOB)")
                db.execute("CREATE TABLE IF NOT EXISTS current_policy(tenant TEXT NOT NULL,origin TEXT NOT NULL,"
                    "policy_sha256 TEXT NOT NULL,observation_id TEXT NOT NULL,PRIMARY KEY(tenant,origin,policy_sha256))")
                db.execute("CREATE TABLE IF NOT EXISTS probes(tenant TEXT NOT NULL,origin TEXT NOT NULL,"
                    "policy_sha256 TEXT NOT NULL,owner TEXT NOT NULL,lease_until REAL NOT NULL,"
                    "PRIMARY KEY(tenant,origin,policy_sha256))")
        except (OSError, ValueError):
            raise _blocked("ROBOTS_CACHE_UNAVAILABLE") from None

    def _safe_paths(self):
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise _blocked("ROBOTS_CACHE_UNSAFE")
        if self.directory.exists():
            info = self.directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise _blocked("ROBOTS_CACHE_UNSAFE")
        for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            if path.exists() or path.is_symlink():
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    # SQLite may unlink its regular journal between these
                    # observations; the main file must still exist on open.
                    if path == self.path:
                        raise _blocked("ROBOTS_CACHE_UNAVAILABLE") from None
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or info.st_mode & 0o022):
                    raise _blocked("ROBOTS_CACHE_UNSAFE")

    @contextmanager
    def _connection(self, deadline=None):
        db = None
        try:
            self._safe_paths()
            before = self.path.stat()
            remaining = deadline - time.monotonic() if deadline is not None else 2.0
            if remaining <= 0:
                raise _blocked("ROBOTS_DEADLINE_EXCEEDED")
            db = sqlite3.connect(self.path, timeout=min(2.0, remaining), isolation_level=None)
            self._safe_paths()
            after = self.path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise _blocked("ROBOTS_CACHE_UNSAFE")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if db is not None and db.in_transaction:
                db.rollback()
            raise _blocked("ROBOTS_CACHE_UNAVAILABLE") from None
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise _blocked("ROBOTS_CLOCK_INVALID")
        return float(now)

    def _record(self, origin, now, *, body=None, result="failure", reason="ROBOTS_FETCH_FAILED",
                http_status=None, retry_after=None, request_count=0, bytes_received=0):
        retry = retry_after if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) else 0.0
        retry = retry if math.isfinite(retry) and retry >= 0 else 0.0
        ttl = self.cache_ttl_seconds if result == "valid" else max(self.failure_ttl_seconds, retry)
        return {"schema": "tiger.robots-observation/v1", "tenant": self.tenant, "origin": origin,
            "policy_sha256": self.policy_sha256, "user_agent": USER_AGENT, "observed_at": now,
            "expires_at": now + ttl, "retry_at": now + ttl if result == "failure" else None,
            "result": result, "reason": reason, "http_status": http_status,
            "body_available": body is not None, "body_sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
            "body_bytes": len(body) if body is not None else None,
            "bytes_received": bytes_received, "request_count": request_count}

    def _decode(self, row, origin, now):
        try:
            identity, serialized, body = row
            record = json.loads(serialized, parse_constant=_invalid_constant)
            if (set(record) != _FIELDS or canonical(record).decode() != serialized or digest(record) != identity
                    or record["schema"] != "tiger.robots-observation/v1"
                    or record["tenant"] != self.tenant or record["origin"] != origin
                    or record["policy_sha256"] != self.policy_sha256 or record["user_agent"] != USER_AGENT
                    or record["result"] not in {"valid", "failure"}
                    or not isinstance(record["reason"], str) or not record["reason"].startswith("ROBOTS_")):
                raise ValueError
            for key in ("observed_at", "expires_at"):
                value = record[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError
            if record["observed_at"] > now + 1 or record["expires_at"] <= record["observed_at"]:
                raise ValueError
            for key in ("bytes_received", "request_count"):
                if type(record[key]) is not int or record[key] < 0:
                    raise ValueError
            if type(record["body_available"]) is not bool:
                raise ValueError
            if record["result"] == "failure":
                if record["retry_at"] != record["expires_at"]:
                    raise ValueError
            elif (record["retry_at"] is not None or record["reason"] not in {"ROBOTS_OBSERVED", "ROBOTS_OBSERVED_DUPLICATE_MEDIA_TYPES"}
                    or record["expires_at"] != record["observed_at"] + self.cache_ttl_seconds):
                raise ValueError
            if record["body_available"]:
                if (not isinstance(body, bytes) or len(body) > MAX_ROBOTS_BYTES
                        or len(body) != record["body_bytes"] or hashlib.sha256(body).hexdigest() != record["body_sha256"]):
                    raise ValueError
            elif body is not None or record["body_sha256"] is not None or record["body_bytes"] is not None:
                raise ValueError
            if record["result"] == "valid" and (not record["body_available"] or record["http_status"] != 200):
                raise ValueError
            return identity, record, body
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise _blocked("ROBOTS_CACHE_TAMPERED") from None

    def _fetch(self, origin, host, transport, deadline):
        try:
            response = transport.request("GET", origin + "/robots.txt", allowed_hosts=(host,),
                deadline=deadline, max_bytes=MAX_ROBOTS_BYTES, _policy_probe=True,
                headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
        except SourceError as error:
            reason = "ROBOTS_BACKOFF" if error.http_status == 429 or error.code == "RATE_LIMITED" else "ROBOTS_FETCH_FAILED"
            if error.code == "RESPONSE_TOO_LARGE":
                reason = "ROBOTS_TOO_LARGE"
            elif error.code in {"DEADLINE_EXCEEDED", "SOURCE_TIMEOUT"}:
                reason = "ROBOTS_FETCH_TIMEOUT"
            return self._record(origin, self._now(), reason=reason, http_status=error.http_status,
                retry_after=error.retry_after, bytes_received=error.bytes_received,
                request_count=error.details.get("request_count", 0)), None
        now = self._now()
        body = response.body
        media_types = [value.split(";", 1)[0].strip().lower()
                       for value in str(response.headers.get("content-type", "")).split(",")]
        if not isinstance(body, bytes) or len(body) > MAX_ROBOTS_BYTES:
            return self._record(origin, now, reason="ROBOTS_TOO_LARGE", http_status=response.status,
                request_count=response.request_count, bytes_received=len(body) if isinstance(body, bytes) else 0), None
        reason = None
        if response.status != 200:
            reason = "ROBOTS_BACKOFF" if response.status == 429 else "ROBOTS_FETCH_FAILED"
        elif response.transport_sha256 != hashlib.sha256(body).hexdigest():
            reason = "ROBOTS_RESPONSE_TAMPERED"
        elif "text/plain" not in media_types or set(media_types) - {"text/plain", "text/html"}:
            reason = "ROBOTS_MEDIA_TYPE_INVALID"
        else:
            try:
                if _origin(response.url)[0] != origin:
                    reason = "ROBOTS_REDIRECT_ORIGIN_DENIED"
                else:
                    _rules(body)
            except SourceError as error:
                reason = "ROBOTS_REDIRECT_ORIGIN_DENIED" if error.details["reason"] == "ROBOTS_URL_INVALID" else error.details["reason"]
        record = self._record(origin, now, body=body, result="failure" if reason else "valid",
            reason=reason or ("ROBOTS_OBSERVED_DUPLICATE_MEDIA_TYPES" if len(media_types) > 1 else "ROBOTS_OBSERVED"), http_status=response.status,
            retry_after=retry_after_seconds(response.headers.get("retry-after")),
            request_count=response.request_count, bytes_received=len(body))
        return record, body

    def _decision(self, identity, record, body, parts, *, cache_hit):
        now = self._now()
        if record["result"] == "failure":
            raise _blocked(record["reason"], retry_after=max(0.0, record["retry_at"] - now),
                http_status=record["http_status"], bytes_received=0 if cache_hit else record["bytes_received"],
                request_count=0 if cache_hit else record["request_count"])
        path = _normalized_path((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        matching = [rule for rule in _rules(body) if _matches(rule.pattern, path)]
        selected = max(matching, key=lambda rule: (rule.specificity, rule.allow)) if matching else None
        if selected is not None and not selected.allow:
            raise _blocked("ROBOTS_DISALLOWED", retry_after=max(0.0, record["expires_at"] - now),
                bytes_received=0 if cache_hit else record["bytes_received"],
                request_count=0 if cache_hit else record["request_count"])
        return {"schema": "tiger.robots-decision/v1", "allowed": True, "reason": "ROBOTS_ALLOWED",
            "origin": record["origin"], "user_agent": USER_AGENT, "observation_id": identity,
            "robots_sha256": record["body_sha256"], "observed_at": record["observed_at"],
            "expires_at": record["expires_at"], "cache_hit": cache_hit,
            "probe_requests": 0 if cache_hit else record["request_count"],
            "probe_bytes": 0 if cache_hit else record["bytes_received"]}

    def check(self, url, transport, deadline=None):
        """Admit a normal GET, or raise FETCH_DENIED with a ROBOTS_* reason.

        Only trusted construction can add an exact-path public contract
        exemption. Requests and robots text cannot add or change exemptions.
        The caller handles all provider budgets and actual-hop host admission.
        """
        origin, host, parts = _origin(url)
        self._safe_paths()
        cutoff = deadline if deadline is not None else time.monotonic() + 25.0
        if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff):
            raise _blocked("ROBOTS_DEADLINE_INVALID")
        if time.monotonic() >= cutoff:
            raise _blocked("ROBOTS_DEADLINE_EXCEEDED")
        for exemption in self.exemptions:
            if origin == exemption["origin"] and parts.path == exemption["exact_path"] and not parts.query:
                return {"schema": "tiger.robots-decision/v1", "allowed": True,
                    "reason": "ROBOTS_PUBLIC_CONTRACT_EXEMPT", "origin": origin, "user_agent": USER_AGENT,
                    "method": "GET", "exact_path": exemption["exact_path"],
                    "contract_sha256": exemption["contract_sha256"], "robots_sha256": None,
                    "cache_hit": False, "probe_requests": 0, "probe_bytes": 0}
        owner = uuid.uuid4().hex
        while True:
            if time.monotonic() >= cutoff:
                raise _blocked("ROBOTS_DEADLINE_EXCEEDED")
            now = self._now()
            with self._connection(cutoff) as db:
                row = db.execute("SELECT o.id,o.record,o.body FROM current_policy c LEFT JOIN observations o "
                    "ON o.id=c.observation_id WHERE c.tenant=? AND c.origin=? AND c.policy_sha256=?",
                    (self.tenant, origin, self.policy_sha256)).fetchone()
                if row is not None:
                    identity, record, body = self._decode(row, origin, now)
                    if record["expires_at"] > now:
                        return self._decision(identity, record, body, parts, cache_hit=True)
                lease = db.execute("SELECT lease_until FROM probes WHERE tenant=? AND origin=? AND policy_sha256=?",
                    (self.tenant, origin, self.policy_sha256)).fetchone()
                if lease is None or lease[0] <= now:
                    db.execute("INSERT INTO probes VALUES(?,?,?,?,?) ON CONFLICT(tenant,origin,policy_sha256) "
                        "DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until",
                        (self.tenant, origin, self.policy_sha256, owner, now + max(0.001, cutoff - time.monotonic())))
                    break
            time.sleep(min(0.025, max(0.0, cutoff - time.monotonic())))
        try:
            record, body = self._fetch(origin, host, transport, cutoff)
            identity = digest(record)
            # The HTTP deadline still forbids another network operation. A
            # bounded local commit preserves the observation even when that
            # request used the final millisecond of the transport deadline.
            with self._connection() as db:
                lease = db.execute("SELECT owner FROM probes WHERE tenant=? AND origin=? AND policy_sha256=?",
                    (self.tenant, origin, self.policy_sha256)).fetchone()
                db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?)", (identity, canonical(record).decode(), body))
                owns_probe = lease is not None and lease[0] == owner
                if owns_probe:
                    db.execute("INSERT INTO current_policy VALUES(?,?,?,?) ON CONFLICT(tenant,origin,policy_sha256) "
                        "DO UPDATE SET observation_id=excluded.observation_id", (self.tenant, origin, self.policy_sha256, identity))
                    db.execute("DELETE FROM probes WHERE tenant=? AND origin=? AND policy_sha256=? AND owner=?",
                        (self.tenant, origin, self.policy_sha256, owner))
            if not owns_probe:
                raise _blocked("ROBOTS_PROBE_OWNERSHIP_LOST", bytes_received=record["bytes_received"],
                    request_count=record["request_count"])
            if record["result"] == "valid" and time.monotonic() >= cutoff:
                raise _blocked("ROBOTS_DEADLINE_EXCEEDED", bytes_received=record["bytes_received"],
                    request_count=record["request_count"])
            return self._decision(identity, record, body, parts, cache_hit=False)
        finally:
            with self._connection() as db:
                db.execute("DELETE FROM probes WHERE tenant=? AND origin=? AND policy_sha256=? AND owner=?",
                    (self.tenant, origin, self.policy_sha256, owner))
