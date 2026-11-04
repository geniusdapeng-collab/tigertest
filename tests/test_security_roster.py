from datetime import date, datetime, timezone
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading_system.security_roster import SecurityRoster, observed_roster
from trading_system.coverage_replay import _market_for_date


def _packet(tmp_path):
    source = b"fixture for dated membership and corporate actions"
    digest = sha256(source).hexdigest()
    (tmp_path / f"{digest}.bin").write_bytes(source)
    cutoff = "2026-05-04T13:00:00+00:00"
    packet = {"schema": "tiger.security-roster.v1", "scope": "us-reviewed-paper",
              "sources": {"fixture": {"url": "https://example.com/evidence", "sha256": digest,
                                      "published_at": cutoff}},
              "sessions": {f"2026-05-0{day}": {"known_at": cutoff, "source_ref": "fixture"}
                           for day in (4, 5, 6, 7, 8)},
              "events": []}

    def event(identity, day, kind, **details):
        packet["events"].append({"security_id": identity, "effective_date": f"2026-05-0{day}",
                                 "known_at": details.pop("known_at", cutoff), "kind": kind, "source_ref": "fixture", **details})

    event("x:one", 4, "listing", symbol="AAA")
    event("x:one", 5, "rename", symbol="BBB", known_at="2026-05-05T21:30:00+00:00")
    event("x:one", 6, "split", ratio=2.0)
    event("x:one", 7, "halt")
    event("x:one", 8, "delist")
    event("x:two", 6, "listing", symbol="AAA")
    return packet


def _write(tmp_path, packet):
    raw = json.dumps(packet).encode()
    path = tmp_path / "roster.json"
    path.write_bytes(raw)
    return path, sha256(raw).hexdigest()


def test_point_in_time_uses_effective_and_known_time_and_survives_ticker_reuse(tmp_path):
    roster = SecurityRoster.load(*_write(tmp_path, _packet(tmp_path)))
    def view(day):
        return roster.on(date(2026, 5, day), datetime(2026, 5, day, 20, tzinfo=timezone.utc))
    assert view(4).symbols == {"AAA": "x:one"}
    assert view(5).symbols == {"AAA": "x:one"}
    assert view(6).symbols == {"BBB": "x:one", "AAA": "x:two"}
    assert view(6).members["BBB"].bars_since == date(2026, 5, 6)
    assert view(7).symbols == {"AAA": "x:two"}
    assert view(7).unavailable == {"BBB": "halted"}
    assert view(8).symbols == {"AAA": "x:two"}
    assert view(8).unavailable == {"BBB": "delisted"}
    with pytest.raises(ValueError, match="coverage"):
        view(9)


def test_packet_verifies_exact_bytes_sources_and_rejects_ambiguous_active_alias(tmp_path):
    packet = _packet(tmp_path)
    path, expected = _write(tmp_path, packet)
    with pytest.raises(ValueError, match="digest"):
        SecurityRoster.load(path, "0" * 64)
    source = tmp_path / (packet["sources"]["fixture"]["sha256"] + ".bin")
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source digest"):
        SecurityRoster.load(path, expected)
    packet = _packet(tmp_path)
    packet["events"].append({**packet["events"][0], "security_id": "x:ambiguous"})
    roster = SecurityRoster.load(*_write(tmp_path, packet))
    with pytest.raises(ValueError, match="Ambiguous"):
        roster.on(date(2026, 5, 4), datetime(2026, 5, 4, 20, tzinfo=timezone.utc))


def test_roster_prevents_earlier_price_regime_from_becoming_tss_input(tmp_path):
    roster = SecurityRoster.load(*_write(tmp_path, _packet(tmp_path)))
    dates = pd.to_datetime(["2026-05-04", "2026-05-05", "2026-05-06"])
    bars = pd.DataFrame({name: [5, 6, 3] for name in ("Open", "High", "Low", "Close", "Volume")}, index=dates)
    view = roster.on(date(2026, 5, 6), datetime(2026, 5, 6, 20, tzinfo=timezone.utc))
    market = {"spy": bars, "tnx": None, "vix": None, "vix9d": None,
              "stock_ohlcv": {"AAA": bars, "BBB": bars}, "sector_etfs": {}}
    observed = _market_for_date(market, dates[-1], roster_view=view)["stock_ohlcv"]
    assert set(observed) == {"AAA", "BBB"}
    assert all(list(frame.index) == [dates[-1]] for frame in observed.values())


def test_live_reviewed_pool_identity_is_explicitly_scoped():
    observed = observed_roster(["AAA", "BBB"], date(2026, 5, 5),
                               datetime(2026, 5, 6, 14, tzinfo=timezone.utc))
    assert observed["symbols"] == {"AAA": "us:AAA", "BBB": "us:BBB"}
    assert observed["basis"] == "reviewed_pool_observed_now"
    assert len(observed["sha256"]) == 64
