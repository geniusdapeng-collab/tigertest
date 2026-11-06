"""Bounded HTTP reads with redirect, origin, credential and deadline discipline."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import ipaddress
import json
import math
import socket
import time
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.auth import AuthBase
from requests.utils import select_proxy
from urllib3.exceptions import HTTPError as Urllib3HTTPError, ReadTimeoutError

from .contracts import SourceError, canonical, credential_free_url, safe_value


_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "x-subscription-token", "cookie"})


class _AnonymousAuth(AuthBase):
    """Truthy no-op auth prevents requests from loading ambient netrc auth."""
    def __call__(self, request):
        for key in list(request.headers):
            if key.lower() in _CREDENTIAL_HEADERS:
                del request.headers[key]
        return request


class _PinnedAdapter(HTTPAdapter):
    """Connect to a validated literal IP while authenticating the original host.

    The original hostname is never handed back to connection/proxy DNS. TLS SNI,
    certificate hostname validation and HTTP Host retain the original origin.
    """
    def __init__(self, pins):
        self.pins = pins
        super().__init__(max_retries=0)

    def _origin(self, url):
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii")
        ip = self.pins.get(host)
        if ip is None or not ipaddress.ip_address(ip).is_global:
            raise SourceError("FETCH_DENIED", delivery="not_sent")
        return parts, host, ip

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        parts, host, ip = self._origin(request.url)
        if hasattr(self, "build_connection_pool_key_attributes"):
            host_params, options = self.build_connection_pool_key_attributes(request, verify, cert)
        else:
            host_params, options = {"scheme": parts.scheme, "host": host, "port": parts.port}, {}
        host_params["host"] = ip
        if parts.scheme == "https":
            options.update(assert_hostname=host, server_hostname=host)
        proxy = select_proxy(request.url, proxies or {})
        manager = self.proxy_manager_for(proxy) if proxy else self.poolmanager
        return manager.connection_from_host(**host_params, pool_kwargs=options)

    def get_connection(self, url, proxies=None):
        # Requests 2.31 calls the legacy method; newer releases use the explicit
        # TLS-context hook above. Both paths connect only to a pinned address.
        parts, host, ip = self._origin(url)
        options = {"assert_hostname": host, "server_hostname": host} if parts.scheme == "https" else {}
        proxy = select_proxy(url, proxies or {})
        manager = self.proxy_manager_for(proxy) if proxy else self.poolmanager
        return manager.connection_from_host(ip, port=parts.port, scheme=parts.scheme, pool_kwargs=options)

    def request_url(self, request, proxies):
        target = super().request_url(request, proxies)
        if target.startswith(("https://", "http://")):
            parts, _, ip = self._origin(request.url)
            literal = "[" + ip + "]" if ":" in ip else ip
            if parts.port:
                literal += ":" + str(parts.port)
            target = urlunsplit((parts.scheme, literal, parts.path, parts.query, ""))
        return target

    def send(self, request, **kwargs):
        parts, host, _ = self._origin(request.url)
        request.headers["Host"] = host + (":" + str(parts.port) if parts.port else "")
        return super().send(request, **kwargs)


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: dict
    body: bytes
    received_at: str
    transport_sha256: str
    request_count: int = 1
    parse_failure_receipt: dict | None = None

    def json(self):
        def pairs(values):
            obj = {}
            for key, value in values:
                if key in obj:
                    raise ValueError
                obj[key] = value
            return obj
        try:
            return json.loads(self.body.decode("utf-8"), object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("CONTRACT_INVALID", delivery="returned",
                              bytes_received=len(self.body)) from None


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.utcoffset() is None:
                return None
            return max(0.0, (parsed - (now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def _request_secrets(headers, params):
    values = set()
    for key, value in (headers or {}).items():
        if key.lower() in _CREDENTIAL_HEADERS and isinstance(value, str):
            values.add(value)
            if key.lower() in {"authorization", "proxy-authorization"} and " " in value:
                values.add(value.split(" ", 1)[1])
    for key, value in (params or {}).items():
        if not credential_free_url("https://credential-reference.invalid/?" + urlencode({key: "placeholder"})):
            for item in value if isinstance(value, (list, tuple)) else (value,):
                if isinstance(item, str):
                    values.add(item)
    return tuple(value for value in values if value)


def _redirect_echoes_secret(url, secrets):
    candidate = html.unescape(url)
    for _ in range(9):
        if any(secret in candidate for secret in secrets):
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    # Ambiguous nested encoding cannot safely be followed.
    return True


class HttpTransport:
    """No retry is hidden here: the owner records every charged/unknown attempt."""

    def __init__(self, *, session=None, allow_local: bool = False,
                 timeout: float = 25.0, max_bytes: int = 32 * 1024 * 1024,
                 deadline: float | None = None, request_policy=None):
        self.session = session or requests.Session()
        # Robots probes have an independent empty cookie jar/header/auth state.
        # Reusing an authenticated Session would leak ambient credentials even
        # when the adapter supplies no Authorization header for that request.
        self._probe_session = requests.Session()
        self._probe_session.auth = _AnonymousAuth()
        self.allow_local = allow_local
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.deadline = deadline
        self.request_policy = request_policy
        self.minimum_interval_seconds = 0.0
        self.robots_required = True
        self._io_budget = None
        self.max_hops = 6
        self.total_request_count = 0
        self.total_bytes_received = 0
        self._pins = {}
        if not allow_local:
            if not callable(getattr(self.session, "mount", None)):
                raise ValueError("Production transport requires a pin-capable requests Session")
            adapter = _PinnedAdapter(self._pins)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)
            self._probe_session.mount("https://", adapter)
            self._probe_session.mount("http://", adapter)

    @contextmanager
    def io_budget(self, *, request_limit, byte_limit):
        previous = self._io_budget
        if previous is not None:
            yield previous
            return
        self._io_budget = {"requests": request_limit, "bytes": byte_limit}
        try:
            yield self._io_budget
        finally:
            self._io_budget = previous

    def _validate(self, url: str, allowed_hosts: tuple[str, ...] = ()) -> None:
        try:
            p = urlsplit(url)
            host = p.hostname
            if (p.scheme not in {"http", "https"} or not host or p.username is not None
                    or p.password is not None or "\\" in url or any(ord(c) < 33 for c in url)
                    or not self.allow_local and p.port not in {None, 80, 443}):
                raise ValueError
            if allowed_hosts and not any(host == x or host.endswith("." + x) for x in allowed_hosts):
                raise ValueError
            if not self.allow_local:
                if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                    raise ValueError
                addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80))
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ValueError
                candidates = [item[4][0] for item in addresses]
                selected = next((value for value in candidates if ":" not in value), candidates[0])
                self._pins[host.encode("idna").decode("ascii")] = selected
        except (ValueError, OSError, UnicodeError):
            raise SourceError("FETCH_DENIED", delivery="not_sent") from None

    def request(self, method: str, url: str, *, params: dict | None = None,
                json_body: dict | None = None, headers: dict | None = None,
                allowed_hosts: tuple[str, ...] = (), max_bytes: int | None = None,
                deadline: float | None = None, allow_not_modified: bool = False,
                _policy_probe: bool = False) -> HttpResponse:
        if method not in {"GET", "POST"}:
            raise SourceError("POLICY_DENIED", delivery="not_sent")
        limit = min(self.max_bytes, max_bytes) if max_bytes is not None else self.max_bytes
        if type(limit) is not int or limit <= 0:
            raise SourceError("BUDGET_EXHAUSTED", delivery="not_sent")
        cutoff = deadline if deadline is not None else self.deadline
        if cutoff is None and self.request_policy is not None:
            cutoff = time.monotonic() + self.timeout
        received = 0
        current = url
        response = None
        sent = 0
        host_token = None
        host_retry_after = 0.0
        start_count = self.total_request_count
        start_bytes = self.total_bytes_received
        protected_values = _request_secrets(headers, params)
        if method == "GET" and self.request_policy is not None:
            headers = dict(headers or {})
            supplied_agent = headers.pop("User-Agent", headers.pop("user-agent", ""))
            headers["User-Agent"] = (supplied_agent if supplied_agent.startswith("WorkLoom-Tiger-Research")
                else "WorkLoom-Tiger-Research/1.0" + (" " + supplied_agent if supplied_agent else ""))
        try:
            for redirect in range(self.max_hops):
                self._validate(current, allowed_hosts)
                remaining = cutoff - time.monotonic() if cutoff is not None else self.timeout
                if remaining <= 0:
                    raise SourceError("DEADLINE_EXCEEDED", delivery="not_sent")
                if (method == "GET" and not _policy_probe and self.robots_required
                        and self.request_policy is not None
                        and callable(getattr(self.request_policy, "check_robots", None))):
                    self.request_policy.check_robots(current, transport=self,
                        deadline=cutoff if cutoff is not None else time.monotonic() + self.timeout)
                if self._io_budget is not None:
                    limit = min(limit, self._io_budget["bytes"])
                    if self._io_budget["requests"] <= 0 or limit <= 0:
                        raise SourceError("BUDGET_EXHAUSTED", delivery="not_sent")
                if self.request_policy is not None:
                    host_token = self.request_policy.before_request(current,
                        deadline=cutoff if cutoff is not None else time.monotonic() + self.timeout,
                        interval_seconds=self.minimum_interval_seconds)
                    remaining = cutoff - time.monotonic() if cutoff is not None else self.timeout
                    if remaining <= 0:
                        raise SourceError("DEADLINE_EXCEEDED", delivery="not_sent")
                sent += 1
                self.total_request_count += 1
                if self._io_budget is not None:
                    self._io_budget["requests"] -= 1
                session = self._probe_session if _policy_probe else self.session
                if _policy_probe:
                    self._probe_session.cookies.clear()
                    headers = {key: value for key, value in (headers or {}).items() if key.lower() not in _CREDENTIAL_HEADERS}
                response = session.request(method, current, params=params,
                    data=canonical(json_body) if json_body is not None else None,
                    headers={"Accept-Encoding": "identity", **(headers or {})},
                    timeout=(min(10.0, remaining), min(self.timeout, remaining)),
                    allow_redirects=False, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or redirect == self.max_hops - 1:
                        raise SourceError("FETCH_DENIED", http_status=response.status_code,
                                          delivery="returned")
                    target = urljoin(current, location)
                    if not credential_free_url(target) or _redirect_echoes_secret(target, protected_values):
                        raise SourceError("FETCH_DENIED", delivery="returned", details={"reason": "REDIRECT_CREDENTIAL_DENIED"})
                    # Never forward an API credential to another origin.
                    if ((urlsplit(target).scheme, urlsplit(target).netloc) !=
                            (urlsplit(current).scheme, urlsplit(current).netloc)) and headers and any(
                            k.lower() in _CREDENTIAL_HEADERS for k in headers):
                        raise SourceError("FETCH_DENIED", delivery="returned")
                    response.close()
                    response = None
                    if self.request_policy is not None:
                        self.request_policy.after_request(host_token)
                        host_token = None
                    current, params = target, None
                    if method != "GET":
                        raise SourceError("FETCH_DENIED", delivery="returned")
                    continue
                status = response.status_code
                if status == 304 and method == "GET" and allow_not_modified:
                    public_headers = {k.lower(): v for k, v in response.headers.items()
                        if k.lower() in {"last-modified", "etag", "retry-after", "date"}}
                    return HttpResponse(current, status, safe_value(public_headers), b"",
                        datetime.now(timezone.utc).isoformat(), hashlib.sha256(b"").hexdigest(), self.total_request_count - start_count)
                if status != 200:
                    code = "RATE_LIMITED" if status == 429 else "SOURCE_AUTH_REQUIRED" if status == 401 else "FETCH_DENIED" if status in {402, 403} else "SOURCE_UNAVAILABLE"
                    host_retry_after = retry_after_seconds(response.headers.get("Retry-After")) or 0.0
                    raise SourceError(code, http_status=status, delivery="returned",
                        retry_after=host_retry_after or None)
                declared_header = response.headers.get("Content-Length")
                declared = None
                if declared_header is not None:
                    if not declared_header.isdecimal():
                        raise SourceError("CONTRACT_INVALID", delivery="returned", details={"reason": "CONTENT_LENGTH_INVALID"})
                    declared = int(declared_header)
                    if declared > limit:
                        raise SourceError("RESPONSE_TOO_LARGE", delivery="returned", bytes_received=0,
                            details={"reason": "CONTENT_LENGTH_EXCEEDS_REMAINING_BYTES"})
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                declared_body = declared if encoding in {"", "identity"} and not response.headers.get("Transfer-Encoding") else None
                chunks = []
                while True:
                    if cutoff is not None and time.monotonic() >= cutoff:
                        raise SourceError("DEADLINE_EXCEEDED", bytes_received=received)
                    if declared_body is not None and received == declared_body:
                        break
                    room = limit - received
                    if room <= 0:
                        # Without a trustworthy entity length there is no
                        # budget left to prove EOF. Reject explicitly instead
                        # of reading one extra chunk or claiming completeness.
                        raise SourceError("RESPONSE_TOO_LARGE", delivery="returned", bytes_received=received,
                            details={"reason": "END_NOT_VERIFIED_WITHIN_REMAINING_BYTES"})
                    chunk = response.raw.read1(min(16384, room), decode_content=True)
                    if not isinstance(chunk, bytes):
                        raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=received,
                            details={"reason": "RESPONSE_BYTES_INVALID"})
                    if not chunk:
                        if declared_body is not None and received != declared_body:
                            raise SourceError("CONTRACT_INVALID", delivery="returned", bytes_received=received,
                                details={"reason": "RESPONSE_BODY_INCOMPLETE"})
                        break
                    received += len(chunk)
                    self.total_bytes_received += len(chunk)
                    if self._io_budget is not None:
                        self._io_budget["bytes"] -= len(chunk)
                    if cutoff is not None and time.monotonic() >= cutoff:
                        raise SourceError("DEADLINE_EXCEEDED", bytes_received=received)
                    if received > limit:
                        raise SourceError("RESPONSE_TOO_LARGE", delivery="returned", bytes_received=received)
                    chunks.append(chunk)
                body = b"".join(chunks)
                public_headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in {
                    "content-type", "content-encoding", "transfer-encoding", "last-modified", "etag", "retry-after", "content-length", "date"}}
                return HttpResponse(current, status, safe_value(public_headers), body,
                    datetime.now(timezone.utc).isoformat(), hashlib.sha256(body).hexdigest(), self.total_request_count - start_count)
        except SourceError as error:
            error.bytes_received = max(error.bytes_received, self.total_bytes_received - start_bytes)
            error.details.update(request_count=self.total_request_count - start_count,
                                 bytes_received=error.bytes_received)
            raise
        except (requests.Timeout, ReadTimeoutError):
            raise SourceError("SOURCE_TIMEOUT", bytes_received=self.total_bytes_received - start_bytes,
                details={"request_count": self.total_request_count - start_count}) from None
        except (requests.RequestException, Urllib3HTTPError, OSError):
            raise SourceError("SOURCE_UNAVAILABLE", bytes_received=self.total_bytes_received - start_bytes,
                details={"request_count": self.total_request_count - start_count}) from None
        finally:
            if response is not None:
                response.close()
            if self.request_policy is not None:
                self.request_policy.after_request(host_token, retry_after=host_retry_after)
        raise SourceError("SOURCE_UNAVAILABLE")
