"""MC011: Tiingo corporate actions must not become strategy price/volume shocks.

HTTP responses below are synthetic contract fixtures, not captured live prices.
Field names follow https://www.tiingo.com/documentation/end-of-day.
The production indicator functions are exercised without changing their formulas.
"""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlparse

import pandas as pd
import pytest

from trading_system import indicators, pipeline
from trading_system.providers.base import is_synthetic
from trading_system.providers.tiingo import TiingoProvider


_ADJUSTED = ("adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume")
_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def _bars(count=60, action=None):
    """Flat adjusted series with a known corporate action five bars from the end."""
    rows = []
    for i, date in enumerate(pd.bdate_range("2024-01-02", periods=count)):
        before = i < count - 5
        scale = {"split": 4.0, "reverse_split": 0.25, "dividend": 1.05}.get(action, 1.0)
        scale = scale if before else 1.0
        volume_scale = scale if action in {"split", "reverse_split"} else 1.0
        rows.append({
            "date": date.strftime("%Y-%m-%dT00:00:00.000Z"),
            "open": 100.0 * scale, "high": 101.0 * scale,
            "low": 99.0 * scale, "close": 100.0 * scale,
            "volume": 4000.0 / volume_scale,
            "adjOpen": 100.0, "adjHigh": 101.0, "adjLow": 99.0,
            "adjClose": 100.0, "adjVolume": 4000.0,
            "divCash": 5.0 if action == "dividend" and i == count - 5 else 0.0,
            "splitFactor": ({"split": 4.0, "reverse_split": 0.25}.get(action, 1.0)
                            if i == count - 5 else 1.0),
        })
    return rows


def _expected(count=60):
    return pd.DataFrame([[100.0, 101.0, 99.0, 100.0, 4000.0]] * count,
                        columns=list(_COLUMNS),
                        index=pd.DatetimeIndex(pd.bdate_range("2024-01-02", periods=count),
                                               name="Date"))


def _http(monkeypatch, *responses):
    pending = iter(responses)
    calls = []

    def urlopen(request, timeout):
        assert timeout == 30
        calls.append(urlparse(request.full_url).path.split("/")[-2])
        response = next(pending)
        if isinstance(response, Exception):
            raise response
        payload = response if isinstance(response, bytes) else json.dumps(response).encode()
        return io.BytesIO(payload)

    monkeypatch.setattr("trading_system.providers.tiingo.urllib.request.urlopen", urlopen)
    return calls


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("TIINGO_API_KEY", "fixture-not-a-credential")
    monkeypatch.setattr("trading_system.providers.tiingo.time.sleep", lambda _: None)
    return TiingoProvider()


def test_no_action_control_preserves_prices_volume_and_sorted_dates(provider, monkeypatch):
    calls = _http(monkeypatch, list(reversed(_bars())))
    actual = provider.ohlcv("PLAIN", days=60)
    pd.testing.assert_frame_equal(actual, _expected(), check_freq=False)
    assert actual.index.tz is None
    assert calls == ["PLAIN"]
    assert provider.name == "tiingo" and not is_synthetic(provider)


@pytest.mark.parametrize("action", ["split", "reverse_split", "dividend"])
def test_corporate_action_is_not_an_atr_or_structure_shock(provider, monkeypatch, action):
    rows = _bars(action=action)
    _http(monkeypatch, rows)
    actual = provider.ohlcv("ACTION", days=60)
    raw = pd.DataFrame({c: [r[c.lower()] for r in rows] for c in _COLUMNS},
                       index=_expected().index)
    # This proves the fixture exercises the original raw-bar failure path.
    assert indicators.true_range(raw).iloc[-5] > 2.0
    assert indicators.atr(raw).iloc[-1] > 2.0
    assert indicators.true_range(actual).iloc[-5] == pytest.approx(2.0)
    assert indicators.atr(actual).iloc[-1] == pytest.approx(2.0)
    features = indicators.extract_structure_features(actual)
    assert features["atr14"] == pytest.approx(2.0)
    assert features["atr50"] == pytest.approx(2.0)
    assert features["key_level"] == pytest.approx(101.0)
    assert features["dd_252"] == pytest.approx(100.0 / 101.0 - 1.0)
    pd.testing.assert_frame_equal(actual, _expected(), check_freq=False)


def test_all_adjusted_fields_are_used_exactly_without_local_reconstruction(provider, monkeypatch):
    rows = _bars()
    supplied = [90.125, 92.25, 89.5, 91.75, 12345.5]
    rows[0].update(dict(zip(_ADJUSTED, supplied)))
    rows[0].update({"open": 1, "high": 2, "low": 0.5, "close": 1,
                    "volume": 1, "splitFactor": 1, "divCash": 0})
    _http(monkeypatch, rows)
    actual = provider.ohlcv("EXACT", days=60)
    assert actual.iloc[0].tolist() == supplied


@pytest.mark.parametrize("field", (*_ADJUSTED, "date"))
def test_one_row_missing_required_field_rejects_whole_ticker(provider, monkeypatch, field):
    rows = _bars()
    del rows[20][field]
    _http(monkeypatch, rows)
    with pytest.raises(RuntimeError, match="tiingo"):
        provider.ohlcv("MISSING", days=60)
    assert "MISSING" not in provider._cache


@pytest.mark.parametrize("field", _ADJUSTED)
@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), float("-inf"),
                                    "bad", "100", True, [], {}, 10**400],
                         ids=["null", "nan", "inf", "negative_inf", "text", "numeric_text",
                              "bool", "array", "object", "overflow"])
def test_invalid_adjusted_cell_never_falls_back_to_valid_raw(provider, monkeypatch,
                                                            field, invalid):
    rows = _bars()
    rows[20][field] = invalid
    _http(monkeypatch, rows)
    with pytest.raises(RuntimeError, match="tiingo.*复权"):
        provider.ohlcv("INVALID", days=60)
    assert "INVALID" not in provider._cache


@pytest.mark.parametrize("field", _ADJUSTED[:4])
@pytest.mark.parametrize("invalid", [0.0, -1.0])
def test_nonpositive_adjusted_prices_are_rejected(provider, monkeypatch, field, invalid):
    rows = _bars()
    rows[20][field] = invalid
    _http(monkeypatch, rows)
    with pytest.raises(RuntimeError, match="tiingo.*复权"):
        provider.ohlcv("BADPRICE", days=60)
    assert "BADPRICE" not in provider._cache


@pytest.mark.parametrize("bad_row", [
    {"adjHigh": 98.0}, {"adjLow": 102.0},
    {"adjOpen": 102.0}, {"adjOpen": 98.0},
    {"adjClose": 102.0}, {"adjClose": 98.0}, {"adjVolume": -1.0},
])
def test_inconsistent_adjusted_bar_is_rejected(provider, monkeypatch, bad_row):
    rows = _bars()
    rows[20].update(bad_row)
    _http(monkeypatch, rows)
    with pytest.raises(RuntimeError, match="tiingo.*复权"):
        provider.ohlcv("BADBAR", days=60)
    assert "BADBAR" not in provider._cache


def test_zero_volume_and_flat_bar_are_valid(provider, monkeypatch):
    rows = _bars()
    rows[20].update({"adjOpen": 100.0, "adjHigh": 100.0, "adjLow": 100.0,
                     "adjClose": 100.0, "adjVolume": 0.0})
    _http(monkeypatch, rows)
    actual = provider.ohlcv("FLAT", days=60)
    assert len(actual) == 60
    assert actual.iloc[20].tolist() == [100.0, 100.0, 100.0, 100.0, 0.0]


@pytest.mark.parametrize("bad_date", [None, "", "NaT", "not-a-date", 0, True])
def test_unverifiable_date_rejects_whole_response(provider, monkeypatch, bad_date):
    rows = _bars()
    rows[20]["date"] = bad_date
    _http(monkeypatch, rows)
    with pytest.raises(RuntimeError, match="tiingo.*日期"):
        provider.ohlcv("BADDATE", days=60)
    assert "BADDATE" not in provider._cache


def test_duplicate_date_rejects_whole_response(provider, monkeypatch):
    rows = _bars()
    rows[20]["date"] = rows[19]["date"]
    _http(monkeypatch, rows)
    with pytest.raises(RuntimeError, match="tiingo.*日期"):
        provider.ohlcv("DUPLICATE", days=60)
    assert "DUPLICATE" not in provider._cache


@pytest.mark.parametrize("payload", [[], None, {}, {"detail": "unavailable"},
                                   [None], ["row"], [1], [[1, 2]]])
def test_empty_or_malformed_payload_is_never_cached(provider, monkeypatch, payload):
    _http(monkeypatch, payload)
    with pytest.raises(RuntimeError, match="tiingo"):
        provider.ohlcv("EMPTY", days=60)
    assert provider._cache == {}


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_failure_propagates_without_cache(provider, monkeypatch, status):
    error = HTTPError("https://example.invalid", status, "fixture failure", {}, None)
    _http(monkeypatch, error)
    with pytest.raises(HTTPError) as exc:
        provider.ohlcv("ERROR", days=60)
    assert exc.value.code == status
    assert provider._cache == {}


@pytest.mark.parametrize("response,exception", [
    (TimeoutError("fixture timeout"), TimeoutError), (b"not JSON", json.JSONDecodeError),
])
def test_transport_or_parse_error_propagates_without_cache(provider, monkeypatch,
                                                         response, exception):
    _http(monkeypatch, response)
    with pytest.raises(exception):
        provider.ohlcv("ERROR", days=60)
    assert provider._cache == {}


def test_cache_reuse_refetch_and_failed_refresh_preserve_valid_adjusted_data(provider, monkeypatch):
    invalid = _bars(count=90, action="split")
    invalid[20]["adjClose"] = None
    calls = _http(monkeypatch, _bars(count=30, action="split"), _bars(action="split"), invalid)
    pd.testing.assert_frame_equal(provider.ohlcv("CACHE", days=30), _expected(30), check_freq=False)
    pd.testing.assert_frame_equal(provider.ohlcv("CACHE", days=30), _expected(30), check_freq=False)
    assert calls == ["CACHE"]
    pd.testing.assert_frame_equal(provider.ohlcv("CACHE", days=60), _expected(), check_freq=False)
    with pytest.raises(RuntimeError, match="tiingo.*复权"):
        provider.ohlcv("CACHE", days=90)
    pd.testing.assert_frame_equal(provider.ohlcv("CACHE", days=60), _expected(), check_freq=False)
    assert calls == ["CACHE", "CACHE", "CACHE"]


def test_callers_cannot_mutate_validated_cache(provider, monkeypatch):
    calls = _http(monkeypatch, _bars(action="split"))
    first = provider.ohlcv("CACHE", days=60)
    first.iloc[0, :] = -1.0
    cached = provider.ohlcv("CACHE", days=60)
    pd.testing.assert_frame_equal(cached, _expected(), check_freq=False)
    cached.iloc[-1, :] = float("nan")
    pd.testing.assert_frame_equal(provider.ohlcv("CACHE", days=60), _expected(), check_freq=False)
    assert calls == ["CACHE"]


def test_batch_continues_after_invalid_ticker_and_preserves_source_lineage(provider, monkeypatch):
    invalid = _bars(action="split")
    del invalid[20]["adjVolume"]
    _http(monkeypatch, invalid, _bars(action="split"))
    monkeypatch.setattr(pipeline, "_LINEAGE", [])
    monkeypatch.setattr(pipeline, "_DATA_ISSUES", [])
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [provider])

    class EmptyPrimary:
        name = "stooq"
        synthetic = False

        def ohlcv_batch(self, tickers, days, *, hard_deadline=None, progress_callback=None):
            return {}

    actual = pipeline._batch_with_fallback(EmptyPrimary(), ["BAD", "GOOD"], 60)
    assert list(actual) == ["GOOD"]
    pd.testing.assert_frame_equal(actual["GOOD"], _expected(), check_freq=False)
    assert ("batch+补1只", "tiingo") in pipeline._LINEAGE
    assert any(item["issue"] == "batch_missing" and item["symbols"] == ["BAD"]
               for item in pipeline._DATA_ISSUES)
    assert "BAD" not in provider._cache


def test_empty_batch_does_not_request_network(provider, monkeypatch):
    calls = _http(monkeypatch)
    assert provider.ohlcv_batch([], days=60) == {}
    assert calls == []


def test_parallel_tickers_have_isolated_adjusted_caches(provider, monkeypatch):
    def urlopen(request, timeout):
        ticker = urlparse(request.full_url).path.split("/")[-2]
        rows = _bars(action="split" if ticker == "SPLIT" else "dividend")
        return io.BytesIO(json.dumps(rows).encode())

    monkeypatch.setattr("trading_system.providers.tiingo.urllib.request.urlopen", urlopen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda t: provider.ohlcv(t, days=60), ["SPLIT", "DIVIDEND"]))
    for result in results:
        pd.testing.assert_frame_equal(result, _expected(), check_freq=False)
    assert set(provider._cache) == {"SPLIT", "DIVIDEND"}


def test_missing_key_keeps_optional_provider_unavailable(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    assert TiingoProvider.available() is False
    with pytest.raises(RuntimeError, match="TIINGO_API_KEY"):
        TiingoProvider()
