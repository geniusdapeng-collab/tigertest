"""Actual loopback HTTP verifies shared attachment-host admission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import threading
import time

import pytest

from test_intelligence_acquisition import web
from trading_system.intelligence.contracts import IntelligenceError, SourceError
from trading_system.intelligence.host_limits import HostRequestPolicy
from trading_system.intelligence.transport import HttpTransport, retry_after_seconds


def test_distinct_discovery_workers_serialize_actual_original_host(tmp_path, web):
    url, routes, calls = web
    guard = threading.Lock()
    observed = {"active": 0, "peak": 0, "starts": []}
    def original(_):
        with guard:
            observed["active"] += 1
            observed["peak"] = max(observed["peak"], observed["active"])
            observed["starts"].append(time.monotonic())
        time.sleep(0.12)
        with guard:
            observed["active"] -= 1
        return 200, "text/plain", "Original evidence"
    routes["/original-a"] = routes["/original-b"] = original
    transports = [HttpTransport(allow_local=True,
        request_policy=HostRequestPolicy(tmp_path, minimum_interval_seconds=0.2)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].request("GET", url + pair[1],
            deadline=time.monotonic() + 5), zip(transports, ["/original-a", "/original-b"])))
    assert len(calls) == 2 and all(result.status == 200 for result in results)
    assert observed["peak"] == 1
    assert observed["starts"][1] - observed["starts"][0] >= 0.19


def test_long_retry_after_applies_to_other_url_and_survives_process_restart(tmp_path, web):
    url, routes, calls = web
    routes["/first-source"] = 429, "text/plain", "later", {"Retry-After": "7200"}
    routes["/other-attachment"] = 200, "text/plain", "Must not send"
    first = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    with pytest.raises(SourceError) as rejected:
        first.request("GET", url + "/first-source", deadline=time.monotonic() + 1)
    assert rejected.value.retry_after == 7200
    restarted = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    with pytest.raises(SourceError) as blocked:
        restarted.request("GET", url + "/other-attachment", deadline=time.monotonic() + 0.1)
    assert blocked.value.code == "RATE_LIMITED" and blocked.value.delivery == "not_sent"
    assert blocked.value.retry_after > 7199
    assert len(calls) == 1 and restarted.total_request_count == 0


def test_redirect_acquires_each_actual_host_and_releases_both(tmp_path, web):
    url, routes, calls = web
    target = url.replace("127.0.0.1", "localhost") + "/original"
    routes["/discovery"] = 302, "text/plain", "", {"Location": target}
    routes["/original"] = 200, "text/plain", "Original"
    transport = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    result = transport.request("GET", url + "/discovery", allowed_hosts=("127.0.0.1", "localhost"),
        deadline=time.monotonic() + 1)
    assert result.body == b"Original" and len(calls) == 2
    with sqlite3.connect(tmp_path / "source-host-limits.sqlite3") as db:
        rows = db.execute("SELECT host,owner,lease_until FROM host_limits ORDER BY host").fetchall()
    assert rows == [("127.0.0.1", None, 0), ("localhost", None, 0)]


def test_crashed_owner_has_finite_lease_and_old_generation_cannot_clear_new(tmp_path):
    policy = HostRequestPolicy(tmp_path, minimum_interval_seconds=0)
    old = policy.before_request("https://official.example/first", deadline=time.monotonic() + 0.05)
    time.sleep(0.06)
    replacement = HostRequestPolicy(tmp_path, minimum_interval_seconds=0)
    new = replacement.before_request("https://official.example/second", deadline=time.monotonic() + 1)
    policy.after_request(old, retry_after=100)
    with sqlite3.connect(policy.path) as db:
        row = db.execute("SELECT owner,next_allowed FROM host_limits").fetchone()
    assert row[0] == new["owner"]
    assert row[1] - datetime.now(timezone.utc).timestamp() < 1
    replacement.after_request(new)


def test_busy_host_cannot_escape_total_deadline_or_count_as_sent(tmp_path, web):
    url, _, calls = web
    policy = HostRequestPolicy(tmp_path, minimum_interval_seconds=0)
    token = policy.before_request(url, deadline=time.monotonic() + 1)
    transport = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(tmp_path, minimum_interval_seconds=0))
    started = time.monotonic()
    with pytest.raises(SourceError) as blocked:
        transport.request("GET", url, deadline=time.monotonic() + 0.05)
    assert blocked.value.code == "DEADLINE_EXCEEDED"
    assert time.monotonic() - started < 0.3 and transport.total_request_count == 0 and not calls
    policy.after_request(token)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "not a date"])
def test_nonfinite_retry_after_is_not_a_schedule(value):
    assert retry_after_seconds(value) is None


def test_tenant_boundary_and_symlink_storage(tmp_path):
    first = HostRequestPolicy(tmp_path, tenant="one", minimum_interval_seconds=0)
    token = first.before_request("https://official.example", deadline=time.monotonic() + 1)
    second = HostRequestPolicy(tmp_path, tenant="two", minimum_interval_seconds=0)
    other = second.before_request("https://official.example", deadline=time.monotonic() + 1)
    second.after_request(other)
    first.after_request(token)
    shortcut = tmp_path / "link"
    shortcut.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(IntelligenceError) as rejected:
        HostRequestPolicy(shortcut)
    assert rejected.value.code == "ACCESS_DENIED"


def test_probe_and_redirect_share_one_total_request_reservation(tmp_path, web):
    url, routes, calls = web
    routes["/robots.txt"] = 200, "text/plain", "User-agent: *\nAllow: /\n"
    routes["/start"] = 302, "text/plain", "", {"Location": "/destination"}
    routes["/destination"] = 200, "text/plain", "Must remain unsent"
    transport = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(
        tmp_path, minimum_interval_seconds=0, respect_robots=True))
    with transport.io_budget(request_limit=2, byte_limit=1024):
        with pytest.raises(SourceError) as denied:
            transport.request("GET", url + "/start", deadline=time.monotonic() + 2)
    assert denied.value.code == "BUDGET_EXHAUSTED"
    assert denied.value.details["request_count"] == transport.total_request_count == 2
    assert [row["path"] for row in calls] == ["/robots.txt", "/start"]


def test_probe_bytes_leave_only_actual_remaining_body_budget(tmp_path, web):
    url, routes, calls = web
    robots = b"User-agent: *\nAllow: /\n"
    routes["/robots.txt"] = 200, "text/plain", robots
    routes["/original"] = 200, "text/plain", b"x" * 64
    transport = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(
        tmp_path, minimum_interval_seconds=0, respect_robots=True))
    with transport.io_budget(request_limit=2, byte_limit=len(robots) + 32):
        with pytest.raises(SourceError) as denied:
            transport.request("GET", url + "/original", deadline=time.monotonic() + 2)
    assert denied.value.code == "RESPONSE_TOO_LARGE"
    assert transport.total_bytes_received == len(robots)
    assert denied.value.bytes_received == len(robots)
    assert len(calls) == transport.total_request_count == 2


def test_duplicate_robots_media_labels_still_require_plain_valid_rules(tmp_path, web):
    url, routes, calls = web
    routes["/robots.txt"] = 200, "text/html, text/plain", "User-agent: *\nDisallow: /private\n"
    routes["/public"] = 200, "text/plain", "Public original"
    transport = HttpTransport(allow_local=True, request_policy=HostRequestPolicy(
        tmp_path, minimum_interval_seconds=0, respect_robots=True))
    assert transport.request("GET", url + "/public", deadline=time.monotonic() + 2).body == b"Public original"
    with pytest.raises(SourceError) as blocked:
        transport.request("GET", url + "/private", deadline=time.monotonic() + 2)
    assert blocked.value.details["reason"] == "ROBOTS_DISALLOWED"
    assert [row["path"] for row in calls] == ["/robots.txt", "/public"]
    with sqlite3.connect(tmp_path / "robots-policy" / "observations.sqlite3") as db:
        assert "ROBOTS_OBSERVED_DUPLICATE_MEDIA_TYPES" in db.execute("SELECT record FROM observations").fetchone()[0]
