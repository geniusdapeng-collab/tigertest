from datetime import datetime, timezone

import pytest

from trading_system.search.models import CleanDocument, RawDocument
from trading_system.search.event_identity import EVENT_CONTRACT, validate_events

CUTOFF = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)


def doc(key, host, period, text, *, published="2026-11-02T14:30:00Z", symbol="AAA"):
    title = f"{symbol} earnings report for the quarter ended {period}"
    return CleanDocument(RawDocument(key, "deepseek_search", title, f"https://{host}/{key}",
                                     text, published), tickers=[symbol], events=["earnings"])


def test_same_report_period_independent_sources_are_eligible():
    docs = [doc("a", "reuters.com", "2026-09-30", "Alpha listed AAA raised cash estimates."),
            doc("b", "bloomberg.com", "September 30, 2026", "Analysts cited stronger AAA sales.")]
    stats = validate_events(docs, CUTOFF)
    assert stats["event_identity"]["contract"] == EVENT_CONTRACT
    assert stats["corroborated"] == 2
    assert all(d.corroborated for d in docs)
    assert stats["event_identity"]["documents"]["a"]["event_id"] == stats["event_identity"]["documents"]["b"]["event_id"]


@pytest.mark.parametrize("case", ["report_period", "syndicated", "byline", "unknown", "future", "stale", "unverified_publisher"])
def test_cannot_fake_a_second_point_in_time_source(case):
    a = doc("a", "reuters.com", "2026-09-30", "AAA raised production estimates after reviewing its sales and raw inventory.")
    b = doc("b", "bloomberg.com", "2026-09-30", "AAA analysts independently predicted stronger orders.")
    if case == "report_period":
        b = doc("b", "bloomberg.com", "2026-06-30", b.raw.content)
    if case == "syndicated":
        b = doc("b", "bloomberg.com", "2026-09-30", a.raw.content)
    if case == "byline":
        b = doc("b", "bloomberg.com", "2026-09-30", "Reporting by Reuters. " + b.raw.content)
    if case == "unknown":
        b = doc("b", "bloomberg.com", "2026-09-30", b.raw.content, published="")
    if case == "future":
        b = doc("b", "bloomberg.com", "2026-09-30", b.raw.content, published="2026-11-04T14:00:01Z")
    if case == "stale":
        b = doc("b", "bloomberg.com", "2026-09-30", b.raw.content, published="2025-11-02T00:00:00Z")
    if case == "unverified_publisher":
        b = doc("b", "unknown.example", "2026-09-30", b.raw.content)
    stats = validate_events([a, b], CUTOFF)
    assert stats["corroborated"] == 0
    assert not a.corroborated and not b.corroborated


def test_copies_never_fill_a_scoring_slot_twice():
    a = doc("a", "reuters.com", "2026-09-30", "AAA raised production estimates.")
    b = doc("b", "bloomberg.com", "2026-09-30", "AAA customers separately confirmed increased orders.")
    c = doc("c", "yahoo.com", "2026-09-30", "Reporting by Reuters. " + a.raw.content)
    stats = validate_events([a, b, c], CUTOFF)
    assert [d.corroborated for d in (a, b, c)] == [True, True, False]
    assert stats["event_identity"]["documents"]["c"]["status"] == "duplicate_origin"


def test_unknown_time_non_event_is_background_only():
    item = doc("a", "reuters.com", "2026-09-30", "AAA background.", published="")
    item.events = []
    stats = validate_events([item], CUTOFF)
    assert not item.corroborated
    assert stats["event_identity"]["documents"]["a"]["status"] == "unknown_publication"


@pytest.mark.parametrize("url,expected", [
    ("https://investor.nvidia.com/news/quarterly-report", True),
    ("https://investor.nvidia.com.attacker.test/news/quarterly-report", False),
    ("https://www.microsoft.com/en-us/store/quarterly-report", False),
    ("https://investor.nvidia.com@attacker.test/news/quarterly-report", False),
])
def test_reviewed_issuer_ir_path_can_supply_one_independent_origin(url, expected):
    company = CleanDocument(
        RawDocument("issuer", "deepseek_search", "NVIDIA reports earnings for the quarter ended 2026-09-30",
                    url, "Management announced stronger operating cash flow.", "2026-11-02T14:30:00Z"),
        tickers=["NVDA"], events=["earnings"])
    media = doc("publisher", "reuters.com", "2026-09-30", "NVDA analysts cited higher orders.", symbol="NVDA")
    stats = validate_events([company, media], CUTOFF, security_ids={"NVDA": "listed:123"})
    assert company.corroborated is expected and media.corroborated is expected
    if expected:
        assert stats["event_identity"]["documents"]["issuer"]["origin"] == "issuer:NVDA"
        assert stats["event_identity"]["documents"]["issuer"]["event_id"].startswith("listed:123/")


def test_sec_filing_and_ir_release_from_same_issuer_are_one_origin():
    from test_model_required_company_evidence import URL, _filing

    filing = _filing("NVDA", url=URL)
    company = CleanDocument(
        RawDocument("issuer", "deepseek_search", "NVIDIA earnings for the quarter ended 2026-09-30",
                    "https://investor.nvidia.com/earnings", "Official operating cash flow commentary.",
                    "2026-11-02T14:30:00Z"),
        tickers=["NVDA"], events=["earnings"])
    submitted = CleanDocument(
        RawDocument("file", "deepseek_search", "NVDA earnings for the quarter ended 2026-09-30",
                    filing["url"], filing["content"], "2026-11-02T14:30:00Z"),
        tickers=["NVDA"], events=["earnings"])
    stats = validate_events([company, submitted], CUTOFF)
    assert stats["corroborated"] == 0
    assert stats["event_identity"]["documents"]["file"]["status"] == "duplicate_origin"
