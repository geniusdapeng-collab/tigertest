"""Immutable own-stock evidence derived only from frozen observed price frames.

The identifier binds the complete typed frame, including its index, columns,
dtypes and all values. A closing reference is neither a news article nor an
authorized entry price. This module performs no inference or external IO.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import re

from ..agent_evidence import normalize
from ..redline.execution import RequiredModelFailed


_FRAME_KEYS = {"__type__", "index", "index_names", "columns", "column_names",
               "dtypes", "values"}
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _bar_date(value: object) -> date:
    if isinstance(value, dict):
        if (set(value) != {"__type__", "value"}
                or value["__type__"] not in {"Timestamp", "datetime", "date"}):
            raise ValueError("Invalid observed date")
        kind, value = value["__type__"], value["value"]
        if kind == "date" and (type(value) is not str or _DATE.fullmatch(value) is None):
            raise ValueError("Invalid observed date")
    if type(value) is not str or _DATE.fullmatch(value[:10]) is None:
        raise ValueError("Invalid observed date")
    if len(value) == 10:
        return date.fromisoformat(value)
    if value[10:11] not in {"T", " "}:
        raise ValueError("Invalid observed timestamp")
    return datetime.fromisoformat(value).date()


def market_snapshots(market_data: dict, symbols: list[str], assessment: dict) -> list[dict]:
    """Bind exact last Close/date and the full canonical frame SHA256.

    Accept native frames or their ``normalize`` snapshots. The frozen research
    date is the upper bound; the benchmark date is not substituted for a stock
    observation. Empty selected symbols need no stock frames and return [].
    """
    try:
        if (type(assessment) is not dict
                or set(assessment) != {"run_date", "market_timezone"}
                or assessment["market_timezone"] != "America/New_York"
                or type(assessment["run_date"]) is not str
                or _DATE.fullmatch(assessment["run_date"]) is None):
            raise ValueError("Invalid frozen assessment")
        research_date = date.fromisoformat(assessment["run_date"])
        if (type(symbols) is not list or len(symbols) > 1000
                or any(type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None
                       for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError("Invalid selected symbols")
        if not symbols:
            return []
        stock_frames = market_data["stock_ohlcv"]
        result = []
        for symbol in symbols:
            frame = normalize(stock_frames[symbol])
            if (type(frame) is not dict or set(frame) != _FRAME_KEYS
                    or frame["__type__"] != "dataframe"
                    or any(type(frame[key]) is not list for key in _FRAME_KEYS - {"__type__"})
                    or not frame["index"] or not frame["columns"]
                    or any(type(column) is not str for column in frame["columns"])
                    or len(set(frame["columns"])) != len(frame["columns"])
                    or len(frame["values"]) != len(frame["index"])
                    or len(frame["dtypes"]) != len(frame["columns"])
                    or frame["columns"].count("Close") != 1
                    or any(type(row) is not list or len(row) != len(frame["columns"])
                           for row in frame["values"])):
                raise ValueError("Invalid observed price frame")
            observed = _bar_date(frame["index"][-1])
            close = frame["values"][-1][frame["columns"].index("Close")]
            if (observed > research_date or type(close) not in {int, float}
                    or not math.isfinite(close) or close <= 0):
                raise ValueError("Invalid observed closing reference")
            series_sha256 = hashlib.sha256(json.dumps(frame, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            if series_sha256 == "0" * 64:
                raise ValueError("Invalid observed series identity")
            as_of = observed.isoformat()
            result.append({"id": "md_" + symbol + "_" + as_of.replace("-", "")
                           + "_" + series_sha256[:24],
                           "symbol": symbol, "as_of": as_of, "close": close,
                           "series_sha256": series_sha256})
        return result
    except (KeyError, ValueError, TypeError, AttributeError, IndexError, OverflowError):
        raise RequiredModelFailed("Mandatory model market evidence is missing or invalid") from None
