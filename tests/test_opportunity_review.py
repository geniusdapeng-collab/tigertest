from datetime import date, datetime, timedelta

import pytest

from trading_system.opportunity_review import _market_review, _render, _validate_review


@pytest.fixture
def inputs():
    start = date(2026, 10, 14)
    index = [{"value": str(start + timedelta(days=i))} for i in range(21)]
    benchmark = {
        "index": index, "columns": ["Close", "High", "Low", "Volume"],
        "values": [[100, 101, 99, 1000] for _ in index],
    }
    stock = {**benchmark, "values": benchmark["values"][:-1] + [[110, 112, 100, 2000]]}
    candidate = {"ticker": "TEST", "price": 110, "key_level": 111, "atr_pct": 0.03,
                 "tss_final": 8.0, "entry_template": ""}
    result = {
        "trade_date": "2026-11-03", "action": "WAIT", "picks": [], "watchlist": [candidate],
        "raw": {
            "freshness": {"market_close": {"session": "2026-11-03", "close_at_utc": "2026-11-03T21:00:00Z"}},
            "rejections": {"candidates": [{"ticker": "TEST", "reasons": ["entry_template_missing"]}]},
            "research_audit": {"funnel": {"universe_size": 3, "gates": {
                "market_data": {"passed": 2}, "hard_filter": {"passed": 2},
            }, "rows": [
                {"ticker": "TEST", "gates": {"hard_filter": {"status": "passed"}}},
                {"ticker": "NEXT", "gates": {"hard_filter": {"status": "passed"}}},
                {"ticker": "DROP", "gates": {"hard_filter": {"status": "unknown"}}},
            ]}},
        },
    }
    return result, {"spy": benchmark, "stock_ohlcv": {"TEST": stock, "NEXT": stock}}


def _review(published="2026-11-02", **source_fields):
    return {"schema": "tiger.issuer-review.v1", "market_date": "2026-11-03",
            "run_id": "run-test", "receipt_sha256": "test-hash", "candidates": [{
        "symbol": "TEST", "thesis": "Issuer evidence", "confirmation": "Hold the level",
        "invalidation": "Break the low", "risk": "Large gap", "sources": [{
            "title": "Issuer report", "url": "https://issuer.example/report", "published_on": published,
            **source_fields,
        }],
    }]}


def test_wait_does_not_remove_research_candidate(inputs):
    market = _market_review(*inputs)
    verified = {"verification": {"receipt_sha256": "test-hash"}}
    draft = _render(market, verified, "run-test")
    reviewed = _render(market, verified, "run-test", _review())
    assert "WAIT" in reviewed and "买入计划 0" in reviewed
    assert "待取证名单" in draft and "Issuer evidence" not in draft
    assert "Issuer evidence" in reviewed and "无 A/B/C 形态" in reviewed
    assert "+10.00 个百分点 | 2.00×" in reviewed
    assert [row["symbol"] for row in market["extra"]] == ["NEXT"]
    assert "| NEXT | 110.00" in reviewed and "DROP" not in reviewed


def test_issuer_evidence_can_promote_a_breadth_candidate(inputs):
    review = _review()
    review["candidates"][0]["symbol"] = "NEXT"
    report = _render(_market_review(*inputs), {"verification": {"receipt_sha256": "test-hash"}},
                     "run-test", review)
    assert "| 1 | NEXT | 110.00 / —" in report
    assert "未进入原精评" in report
    assert "WAIT" in report and "买入计划 0" in report


def test_issuer_review_is_bound_to_exact_run(inputs):
    with pytest.raises(ValueError, match="this exact verified run"):
        _render(_market_review(*inputs), {"verification": {"receipt_sha256": "different"}},
                "run-test", _review())


@pytest.mark.parametrize(("published", "published_at", "allowed"), [
    ("2026-11-02", None, True),
    ("2026-11-03", None, False),
    ("2026-11-03", "2026-11-03T15:59:00-05:00", True),
    ("2026-11-03", "2026-11-03T16:05:44-05:00", False),
    ("2026-11-03", "2026-11-03T15:59:00", False),
    ("2026-11-04", None, False),
])
def test_company_evidence_obeys_market_close(published, published_at, allowed):
    market = {"cutoff": datetime.fromisoformat("2026-11-03T16:00:00-05:00"), "cards": {"TEST": {}}}
    review = _review(published, **({"published_at": published_at} if published_at else {}))
    if allowed:
        assert _validate_review(review, market, market["cards"]) == review["candidates"]
    else:
        with pytest.raises(ValueError, match="publication time|post-close"):
            _validate_review(review, market, market["cards"])


def test_missing_and_misaligned_data_are_disclosed(inputs):
    result, market = inputs
    market["stock_ohlcv"]["TEST"]["index"] = market["spy"]["index"][:-1]
    review = _market_review(result, market)
    assert not review["cards"] and "TEST" in review["unavailable"][0]
    with pytest.raises(ValueError, match="Missing or duplicate verified candidate"):
        _validate_review(_review(), review, review["cards"])


def test_conflicting_score_close_is_rejected(inputs):
    result, market = inputs
    result["watchlist"][0]["price"] = 99
    with pytest.raises(ValueError, match="Frozen close differs"):
        _market_review(result, market)


def test_duplicate_candidate_is_rejected_even_if_data_missing(inputs):
    result, market = inputs
    result["watchlist"] *= 2
    market["stock_ohlcv"].clear()
    with pytest.raises(ValueError, match="Duplicate symbol"):
        _market_review(result, market)
