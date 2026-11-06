"""Durable per-host admission for every actual monitored HTTP hop.

The watch queue limits work units. This smaller ledger also covers original
article and attachment hosts reached by different registered discovery sources.
It never adds a request, retries a request, or changes a source allowlist.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from .contracts import IntelligenceError, SourceError, identifier


class HostRequestPolicy:
    def __init__(self, root, *, tenant="public", minimum_interval_seconds=1.0,
                 respect_robots=False, public_api_exemptions=()):
        self.tenant = identifier(tenant, "tenant")
        if (isinstance(minimum_interval_seconds, bool)
                or not isinstance(minimum_interval_seconds, (int, float))
                or not math.isfinite(minimum_interval_seconds)
                or not 0 <= minimum_interval_seconds <= 3600):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid host request interval")
        self.minimum_interval_seconds = minimum_interval_seconds
        self.root = Path(root).absolute()
        if any(path.is_symlink() for path in [self.root, *self.root.parents]):
            raise IntelligenceError("ACCESS_DENIED", "Source host ledger cannot use symlink paths")
        self.path = self.root / "source-host-limits.sqlite3"
        if self.path.is_symlink() or self.path.exists() and self.path.stat().st_nlink != 1:
            raise IntelligenceError("ACCESS_DENIED", "Unsafe source host ledger")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._connection() as db:
                db.execute("CREATE TABLE IF NOT EXISTS host_limits(tenant TEXT NOT NULL,host TEXT NOT NULL,"
                    "next_allowed REAL NOT NULL,owner TEXT,lease_until REAL NOT NULL,PRIMARY KEY(tenant,host))")
            os.chmod(self.path, 0o600)
        except OSError:
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot initialize source host ledger") from None
        if respect_robots:
            from .robots_policy import RobotsPolicy
            self.robots = RobotsPolicy(root, tenant=tenant, public_api_exemptions=public_api_exemptions)
        else:
            self.robots = None

    def check_robots(self, url, *, transport, deadline):
        if self.robots is not None:
            return self.robots.check(url, transport=transport, deadline=deadline)

    @contextmanager
    def _connection(self, *, deadline=None):
        db = None
        try:
            remaining = deadline - time.monotonic() if deadline is not None else 2.0
            if remaining <= 0:
                raise SourceError("DEADLINE_EXCEEDED", delivery="not_sent")
            db = sqlite3.connect(self.path, timeout=min(2.0, remaining), isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None and db.in_transaction:
                db.rollback()
            if deadline is not None and time.monotonic() >= deadline:
                raise SourceError("DEADLINE_EXCEEDED", delivery="not_sent") from None
            raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Source host admission transaction failed") from None
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def before_request(self, url, *, deadline, interval_seconds=0):
        host = urlsplit(url).hostname
        if not host:
            raise SourceError("FETCH_DENIED", delivery="not_sent")
        host = host.encode("idna").decode("ascii").lower()
        interval = max(self.minimum_interval_seconds, interval_seconds)
        owner = uuid.uuid4().hex
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceError("DEADLINE_EXCEEDED", delivery="not_sent")
            now = datetime.now(timezone.utc).timestamp()
            with self._connection(deadline=deadline) as db:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("DEADLINE_EXCEEDED", delivery="not_sent")
                now = datetime.now(timezone.utc).timestamp()
                row = db.execute("SELECT next_allowed,owner,lease_until FROM host_limits WHERE tenant=? AND host=?",
                    (self.tenant, host)).fetchone()
                cooldown = max(0.0, row[0] - now) if row else 0.0
                busy = row is not None and row[1] is not None and row[2] > now
                if not busy and cooldown <= 0:
                    # The owning HTTP request has the same absolute monotonic
                    # deadline. A crash leaves only this finite UTC lease.
                    db.execute("INSERT INTO host_limits VALUES(?,?,?,?,?) ON CONFLICT(tenant,host) "
                        "DO UPDATE SET next_allowed=excluded.next_allowed,owner=excluded.owner,lease_until=excluded.lease_until",
                        (self.tenant, host, now + interval, owner, now + remaining))
                    return {"host": host, "owner": owner}
            if cooldown >= remaining:
                raise SourceError("RATE_LIMITED", delivery="not_sent", retry_after=cooldown,
                    details={"reason": "SHARED_HOST_BACKOFF"})
            # Poll an active request's release; never sleep for its entire
            # pessimistic lease or round down a long Retry-After interval.
            time.sleep(min(0.1 if busy else cooldown, remaining))

    def after_request(self, token, *, retry_after=0):
        if token is None:
            return
        if not math.isfinite(retry_after) or retry_after < 0:
            raise IntelligenceError("CONTRACT_INVALID", "Invalid host retry interval")
        now = datetime.now(timezone.utc).timestamp()
        with self._connection() as db:
            # A late request cannot clear a replacement generation's lease.
            db.execute("UPDATE host_limits SET owner=NULL,lease_until=0,next_allowed=MAX(next_allowed,?) "
                "WHERE tenant=? AND host=? AND owner=?",
                (now + retry_after, self.tenant, token["host"], token["owner"]))


def bind_host_policy(adapters, policy):
    for adapter in adapters.values():
        transport = getattr(adapter, "transport", None)
        if transport is not None and hasattr(transport, "request_policy"):
            transport.request_policy = policy
    return adapters
