"""Completed US cash sessions and point-in-time daily observations for Tiger paper."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from .calendar import is_trading_day
from .redline.execution import DAILY_MACRO_OBSERVATION_CONTRACT, StrictObservationError

MARKET_CLOSE_CONTRACT = "us-cash-session-close/v1"
SOURCE_PATH = "trading_system/market_close.py"
_EASTERN = ZoneInfo("America/New_York")
# Published NYSE cash calendars: 2024–2026 (ICE 2023 release) and 2026–2028
# (NYSE Hours & Calendars). Nasdaq also observed the Carter closure.
_EARLY_CLOSES = frozenset(map(date.fromisoformat, (
    "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24", "2027-11-26", "2028-07-03", "2028-11-24",
)))
_EXCEPTIONS = frozenset(map(date.fromisoformat, (
    "2025-01-09", "2028-01-17", "2028-02-21", "2028-04-14",
    "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04", "2028-11-23",
    "2028-12-25",
)))


def _scheduled_close(day: date) -> datetime | None:
    if is_trading_day(day) and day not in _EXCEPTIONS:
        return datetime.combine(day, time(13 if day in _EARLY_CLOSES else 16), _EASTERN)
    return None


def scheduled_open(day: date) -> datetime | None:
    """US cash opening for a verified trading session, including early-close days."""
    if 2024 <= day.year <= 2028 and _scheduled_close(day) is not None:
        return datetime.combine(day, time(9, 30), _EASTERN)
    return None


def next_scheduled_close(market_date: date) -> datetime:
    """Cash close of the first session strictly after the research date."""
    day = market_date + timedelta(days=1)
    while 2024 <= day.year <= 2028:
        close = _scheduled_close(day)
        if close is not None:
            return close
        day += timedelta(days=1)
    raise StrictObservationError("Plan expiry is outside the verified US cash session calendar")


def completed_session(admitted_at: datetime) -> datetime:
    """The latest scheduled cash close strictly earlier than admission."""
    if not isinstance(admitted_at, datetime) or admitted_at.utcoffset() is None:
        raise StrictObservationError("An aware admission timestamp is required for market close alignment")
    eastern = admitted_at.astimezone(_EASTERN)
    day = eastern.date()
    while 2024 <= day.year <= 2028:
        close = _scheduled_close(day)
        if close is not None and eastern > close:
            return close
        day -= timedelta(days=1)
    raise StrictObservationError("Frozen admission is outside the verified US cash session calendar")


def close_metadata(close: datetime, *, macro_daily_labels=DAILY_MACRO_OBSERVATION_CONTRACT) -> dict:
    if macro_daily_labels not in {None, DAILY_MACRO_OBSERVATION_CONTRACT}:
        raise StrictObservationError("Unknown macro daily observation contract")
    value = {"contract": MARKET_CLOSE_CONTRACT, "session": close.date().isoformat(),
            "close_at_utc": close.astimezone(timezone.utc).isoformat(),
            "bar_basis": "provider_daily_after_scheduled_close"}
    if macro_daily_labels is not None:
        value["macro_daily_labels"] = macro_daily_labels
    return value


def macro_limits(close: datetime, admitted_at: datetime) -> dict[str, date]:
    # Cboe disseminates VIX-family spot values through 4:15 ET (1:15 on
    # abbreviated equity-option sessions). A provisional same-date VIX daily
    # observation is ineligible while that window is still open.
    day = close.date()
    prior = day - timedelta(days=1)
    vol_date = day if admitted_at > close + timedelta(minutes=15) else prior
    # Use the later known publication boundary across the rate proxies. The
    # official fallback is a date-labeled Fed H.15 series (16:15 ET release).
    rates_publish = datetime.combine(day, time(16, 15), _EASTERN)
    rates_date = day if admitted_at > rates_publish else prior
    return {"tnx": rates_date, "vix": vol_date, "vix9d": vol_date}


def requires_foreign_calendar(symbol: str) -> bool:
    """Known overseas tech symbols require a separate local-session calendar."""
    return symbol.endswith((".KS", ".TW"))


def close_observation(value, cutoff: date, *, exact: bool):
    """Project a normalized daily series. None means the needed bar is absent."""
    import pandas as pd

    expected = pd.DataFrame if exact else pd.Series
    if not isinstance(value, expected) or value.empty:
        return None
    index = value.index
    if (not isinstance(index, pd.DatetimeIndex) or index.hasnans
            or index.tz is not None or not index.is_monotonic_increasing
            or not index.is_unique or not index.equals(index.normalize())):
        raise StrictObservationError("Daily close feed contains unverifiable session labels")
    bounded = value.loc[index.date <= cutoff]
    if not exact:
        bounded = bounded[bounded.map(_positive_finite)]
        return bounded if len(bounded) and (cutoff - bounded.index[-1].date()).days <= 5 else None
    if bounded.empty or bounded.index[-1].date() != cutoff:
        return None
    if not value.columns.is_unique or not {"Open", "High", "Low", "Close", "Volume"}.issubset(value.columns):
        return None
    last = bounded.iloc[-1]
    if not all(_positive_finite(last[field]) for field in ("Open", "High", "Low", "Close", "Volume")):
        return None
    return bounded


def _positive_finite(value) -> bool:
    from numbers import Real

    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def close_chart(frame: dict) -> list[list]:
    """Project the frozen signal benchmark into its public chart format."""
    close_column = frame["columns"].index("Close")
    return [[bar["value"][:10], round(float(row[close_column]), 2)]
            for bar, row in zip(frame["index"], frame["values"])
            if _positive_finite(row[close_column])]


def _snapshot_window(raw, cutoff: date, run_date: date, *, exact: bool,
                     macro_daily_labels=None):
    """Rebuild the admissible typed slice directly from an I/O snapshot."""
    if not isinstance(raw, dict) or raw.get("__type__") != ("dataframe" if exact else "series"):
        return None
    if macro_daily_labels not in {None, DAILY_MACRO_OBSERVATION_CONTRACT}:
        raise ValueError("Unknown macro daily observation contract")
    labels, values = [], []
    previous = date.min
    close_index = None
    if exact:
        columns = raw["columns"]
        fields = ("Open", "High", "Low", "Close", "Volume")
        if len(set(columns)) != len(columns) or not all(field in columns for field in fields):
            return None
        close_index = [columns.index(field) for field in fields]
    for label, value in zip(raw["index"], raw["values"], strict=True):
        if label["__type__"] != "Timestamp":
            raise ValueError("Non-daily observation index")
        stamp = datetime.fromisoformat(label["value"])
        if not exact and macro_daily_labels == DAILY_MACRO_OBSERVATION_CONTRACT:
            # Independent reconstruction deliberately does not call the runtime
            # projector. Source midnight establishes a daily calendar label;
            # retain that date and never borrow the previous NY date from UTC.
            if stamp.time() != time(0):
                raise ValueError("Macro source observation is not a daily session label")
            stamp = stamp.replace(tzinfo=None)
        elif stamp.tzinfo is not None:
            stamp = stamp.astimezone(_EASTERN).replace(tzinfo=None)
        day = stamp.date()
        if stamp.time() != time(0) or not previous < day <= run_date:
            raise ValueError("Invalid market observation index")
        previous = day
        if day <= cutoff and (exact or _positive_finite(value)):
            labels.append({"__type__": "Timestamp", "value": stamp.isoformat()})
            values.append(value)
    if not labels:
        return None
    last = date.fromisoformat(labels[-1]["value"][:10])
    if exact:
        if last != cutoff or not all(_positive_finite(values[-1][i]) for i in close_index):
            return None
    elif (cutoff - last).days > 5:
        return None
    return {**raw, "index": labels, "values": values}


def _close_series(frame: dict) -> dict:
    column = frame["columns"].index("Close")
    return {"__type__": "series", "name": "Close", "index": frame["index"],
            "index_names": frame["index_names"], "dtype": frame["dtypes"][column],
            "values": [row[column] for row in frame["values"]]}


def verify_close_evidence(run, admission: dict, freshness: dict) -> None:
    """Validate the frozen cutoff, selected slices and rendered benchmark input."""
    from .agent_evidence import EvidenceInvalid, read_bytes, read_snapshot, strict_json

    try:
        clock = admission["clock"]
        run_date = date.fromisoformat(clock["run_date"])
        admitted_at = datetime.fromisoformat(clock["admitted_at_utc"])
        close = completed_session(admitted_at)
        session = close.date()
        limits = macro_limits(close, admitted_at)
        rows = (strict_json(line) for line in read_bytes(run, "events.jsonl").splitlines())
        by_stage = {row["stage"]: row for row in rows if row["kind"] == "end"
                    and row["stage"] in {"data.prepare", "report.html"}}

        def snapshot(stage, ref, role):
            return read_snapshot(run, ref, run_id=run.name, stage=stage, role=role)

        prepared = by_stage["data.prepare"]
        bound = snapshot("data.prepare", prepared["input_ref"], "input")
        stage = snapshot("data.prepare", prepared["output_ref"], "output")
        data = stage["value"]["market_data"]
        macro_daily_labels = bound.get("market_close", {}).get("macro_daily_labels")
        meta = close_metadata(close, macro_daily_labels=macro_daily_labels)
        if (bound["market_close"] != meta or data["freshness"] != freshness
                or freshness["benchmark_last_bar"] != meta["session"]
                or type(freshness["benchmark_lag_days"]) is not int or freshness["benchmark_lag_days"]):
            raise ValueError("Market session metadata differs")
        stock, etfs = data["stock_ohlcv"], data["sector_etfs"]
        if data["universe_closes"] != {symbol: _close_series(frame) for symbol, frame in stock.items()}:
            raise ValueError("Universe closes differ from the aligned stock frames")
        tickers = set(stage["value"]["all_tickers"])
        sectors = set(bound["sector_symbols"])
        missing = {"etf_missing": sorted(sectors - etfs.keys()),
                   "stock_missing": sorted(tickers - stock.keys())}
        if (not stock or not stock.keys() <= tickers or not etfs.keys() <= sectors
                or len(etfs) < (len(sectors) * 2 + 2) // 3
                or len(data["spy"]["index"]) < 260 or len(data["tnx"]["index"]) < 100
                or len(data["vix"]["index"]) < 30):
            raise ValueError("Market coverage differs")
        macro_bars = {key: (data[key]["index"][-1]["value"][:10] if data[key] else None) for key in limits}
        if freshness["market_close"] != {**meta, **missing, "macro_last_bars": macro_bars,
                "benchmark_chart": {"SPY": "signal_benchmark", "QQQ": "not_collected"}}:
            raise ValueError("Market session disclosure differs")

        # Match each accepted frame to an actual provider return (same symbol,
        # method and requested history). Report integrity must not rest on the
        # self-reported dates of already-processed frames.
        expected = {
            ("ohlcv", bound["benchmarks"]["index"]): [("spy", data["spy"], session, True)],
            ("rate_yield_for", bound["benchmarks"]["rate"]): [("tnx", data["tnx"], limits["tnx"], False)],
            ("vol_index_for", bound["benchmarks"]["vol"]): [("vix", data["vix"], limits["vix"], False)],
        }
        if data["vix9d"] is not None:
            expected[("vol_index_for", bound["benchmarks"]["vol_short"])] = [
                ("vix9d", data["vix9d"], limits["vix9d"], False)]
        for group, frames in (("etf", etfs), ("stock", stock)):
            for symbol, frame in frames.items():
                expected.setdefault(("ohlcv_batch", symbol), []).append(
                    (f"{group}:{symbol}", frame, session, True))
        observed = set()
        for call in stage["io_calls"]:
            method = call["method"].rsplit(".", 1)[-1]
            if not call["method"].startswith("provider.") or "output_ref" not in call:
                continue
            if method not in {"ohlcv", "ohlcv_batch", "rate_yield_for", "vol_index_for"}:
                continue
            request = snapshot("data.prepare", call["input_ref"], "input")
            if request["kwargs"].get("days") != bound["days"]:
                continue
            response = snapshot("data.prepare", call["output_ref"], "output")
            if method == "ohlcv_batch":
                symbols = request["args"][0]
                results = response if isinstance(response, dict) else {}
            else:
                symbol = request["args"][0]
                symbols, results = [symbol], {symbol: response}
            for symbol in symbols:
                for key, frame, limit, exact in expected.get((method, symbol), ()):
                    if (key not in observed and _snapshot_window(results.get(symbol), limit,
                            run_date, exact=exact, macro_daily_labels=macro_daily_labels) == frame):
                        observed.add(key)
        if observed != {key for entries in expected.values() for key, *_ in entries}:
            raise ValueError("Market frames differ from their frozen provider observations")

        chart_stage = by_stage["report.html"]
        chart = snapshot("report.html", chart_stage["output_ref"], "output")
        report_input = snapshot("report.html", chart_stage["input_ref"], "input")
        report_path = f"work/reports/日报_{run_date:%Y%m%d}.html"
        if (report_input["benchmarks"] != ["SPY"]
                or [call["method"] for call in chart["io_calls"]] != ["file.write_text"]
                or chart["value"]["benchmarks"] != {"SPY": close_chart(data["spy"])}
                or chart["value"]["html"]["text"].encode("utf-8") != read_bytes(run, report_path)):
            raise ValueError("Report benchmark differs from the signal series")
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, StrictObservationError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Frozen completed-session market evidence differs",
                              stage="data.prepare") from exc
