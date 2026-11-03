import pandas as pd
import pytest

from trading_system.coverage_replay import _asof, _bar, _periods, _period_dates


def test_signal_slice_ignores_future_rows_and_keeps_daily_date_requirement():
    dates = pd.to_datetime(["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"])
    frame = pd.DataFrame({"Open": [10, 11, 12, 999], "High": [12, 13, 14, 1000],
                          "Low": [9, 10, 11, 998], "Close": [11, 12, 13, 999],
                          "Volume": [100, 110, 120, 99999]}, index=dates)
    cutoff = dates[2]
    assert _asof(frame, cutoff).equals(frame.iloc[:3])
    altered = frame.copy()
    altered.loc[dates[-1], "Close"] = -123
    assert _asof(altered, cutoff).equals(_asof(frame, cutoff))
    assert _asof(frame.drop(index=cutoff), cutoff, exact=True) is None
    assert _bar(frame.drop(index=cutoff), cutoff) is None
    assert _bar(frame, cutoff).close == 13
    assert _bar(frame, cutoff).adv == (11 * 100 + 12 * 110) / 2
    changed_same_session = frame.copy()
    changed_same_session.loc[cutoff, ["Close", "Volume"]] = [999, 100_000]
    assert _bar(changed_same_session, cutoff).adv == _bar(frame, cutoff).adv


def test_periods_are_nonoverlapping_and_exclude_unfinished_admission_date():
    first = "2026-06-01:2026-06-05"
    second = "2026-10-12:2026-10-16"
    assert len(_periods([second, first])) == 2
    with pytest.raises(ValueError, match="overlap"):
        _periods([first, "2026-06-05:2026-06-09"])
    with pytest.raises(ValueError, match="earlier than the admission date"):
        _period_dates(pd.bdate_range("2026-10-12", "2026-10-16"),
                      (pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-16")),
                      pd.Timestamp("2026-10-16"))
