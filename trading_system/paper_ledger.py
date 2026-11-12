"""Point-in-time market observations and append-only paper execution facts."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import math
from numbers import Real

from . import config
from .agent_evidence import digest, json_bytes
from .market_close import completed_session, next_scheduled_close, scheduled_open

SOURCE_PATH = "trading_system/paper_ledger.py"
CONTRACT = "tiger.paper-execution.v1"


def _positive(value) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def identity(value) -> str:
    return digest(json_bytes(value))


class PaperObservations:
    """Freeze opening evidence independently of completed OHLC and exit costs."""

    def __init__(self, admitted_at, fetch):
        self.admitted_at = admitted_at
        self.completed_session = completed_session(admitted_at).date()
        self.fetch = fetch
        self.frames = {}
        self.selected = {}
        self.earnings_dates = {}

    def opening_ready(self, day):
        start = scheduled_open(day)
        return start is not None and self.admitted_at >= start

    def _row(self, ticker, day):
        if ticker not in self.frames:
            self.frames[ticker] = self.fetch(ticker)
        frame = self.frames[ticker]
        if frame is None or frame.empty:
            return None, None
        if (not frame.index.is_unique or not frame.index.is_monotonic_increasing
                or not frame.index.equals(frame.index.normalize()) or not frame.columns.is_unique
                or not {"Open", "High", "Low", "Close"}.issubset(frame.columns)):
            raise ValueError("Paper quote dates or columns are ambiguous")
        past = frame[frame.index.date <= day]
        if len(past) == 0 or past.index[-1].date() != day:
            return None, None
        return past.iloc[-1], past.iloc[:-1]

    def opening(self, ticker, day):
        from .simulator import Bar

        if not self.opening_ready(day):
            return None
        row, _ = self._row(ticker, day)
        if row is None or not _positive(row["Open"]):
            return None
        value = float(row["Open"])
        iso = day.isoformat()
        # Only Open is admitted here. The compatibility Bar is used by the
        # existing allocation function, which consumes Open and date only.
        self.selected.setdefault(ticker, {}).setdefault("open", {})[iso] = {"open": value, "date": iso}
        return Bar(value, 0, 0, 0, date=iso), iso

    def completed(self, ticker, day):
        from .simulator import Bar

        if day > self.completed_session or scheduled_open(day) is None:
            return None
        row, history = self._row(ticker, day)
        if row is None or not all(_positive(row[key]) for key in ("Open", "High", "Low", "Close")):
            return None
        opening, high, low, close = [float(row[key]) for key in ("Open", "High", "Low", "Close")]
        if not low <= min(opening, close) <= max(opening, close) <= high:
            return None
        adv = (float((history["Close"] * history["Volume"]).tail(20).mean())
               if "Volume" in history else 0.0)
        iso = day.isoformat()
        bar = Bar(opening, high, low, close, adv=adv if _positive(adv) else 0.0, date=iso)
        self.selected.setdefault(ticker, {}).setdefault("completed", {})[iso] = asdict(bar)
        return bar, iso


class PaperFacts:
    def __init__(self, state, run_id, references):
        self.run_id = run_id
        self.references = references
        if state.setdefault("paper_contract", CONTRACT) != CONTRACT:
            raise ValueError("Unknown paper account contract")
        self.signals = state.setdefault("paper_signals", {})
        self.events = state.setdefault("paper_events", [])
        self.existing = {event["event_id"]: event for event in self.events}
        self.new = []
        self.missing = {}

    def register(self, pending, result, pick, date_string, earnings_dates):
        session = date.fromisoformat(date_string)
        planned = next_scheduled_close(session).date().isoformat()
        if pending["entry_budget"]["execution_date"] != planned:
            raise ValueError("Frozen opening budget differs from the cash session calendar")
        identity_map = result.raw.get("security_identities") or result.raw.get("security_roster", {}).get("symbols", {})
        security_id = identity_map.get(pick.ticker, "us:" + pick.ticker)
        sid = identity({"security_id": security_id, "signal_session": date_string,
                        "account_id": "public-usd", "policy": CONTRACT})
        if sid in self.signals:
            return None
        signal = {"signal_id": sid, "run_id": self.run_id, "security_id": security_id,
                  "ticker": pick.ticker, "account_id": "public-usd", "policy": CONTRACT,
                  "signal_session": date_string, "planned_open_session": planned,
                  "template": pick.entry_template, "entry_ref": pick.entry_price,
                  "stop": pick.stop_price, "shares": pick.shares, "risk_usd": pick.risk_usd,
                  "entry_budget": pending["entry_budget"],
                  "sector": pick.sector or "", "chain": pick.chain or "",
                  "tss_final": pick.tss_final, "tos": pick.tos, "mode": pick.card,
                  "mrs_star": result.mrs.mrs_star if result.mrs else None,
                  "action": result.action, "source": pending["source"],
                  "review_snapshot": _review_snapshot(result, pick.ticker, session, earnings_dates),
                  **self.references}
        self.signals[sid] = signal
        self.emit(sid, "registered", date_string)
        return sid

    def emit(self, sid, kind, session, *, reason=None, bar=None, fill=None, close=None):
        if sid is None:
            return
        if sid not in self.signals:
            raise ValueError("Paper event refers to an unknown signal")
        key = {"account_id": "public-usd", "signal_id": sid,
               "event_type": kind, "session": session}
        event_id = identity(key)
        if event_id in self.existing:
            if reason == "BAR_UNAVAILABLE":
                self.missing[event_id] = self.existing[event_id]
            return
        event = {"event_id": event_id, **key, "run_id": self.run_id,
                 "account_version_before": self.references.get("account_version_before"),
                 "reason": reason}
        if bar is not None:
            evidence = ({"open": bar.open, "date": bar.date}
                        if kind in {"filled", "rejected"} else asdict(bar))
            event["market_observation_sha256"] = identity(evidence)
        if fill is not None:
            event["fill"] = fill
        if close is not None:
            event["close"] = close
        self.events.append(event)
        self.new.append(event)
        self.existing[event_id] = event
        if reason == "BAR_UNAVAILABLE":
            self.missing[event_id] = event


def required_sessions(after, through):
    """Contiguous dates of the verified exchange calendar, in execution order."""
    day = after + timedelta(days=1)
    while day <= through:
        if scheduled_open(day) is not None:
            yield day
        day += timedelta(days=1)


def _review_snapshot(result, ticker, session, earnings_dates):
    rationale = result.raw["pick_rationale"][ticker]
    shs = rationale.get("gate", {}).get("shs", {})
    setup = rationale["entry_setup"]
    snapshot = {}
    if type(shs.get("ok")) is bool:
        snapshot["mainline_eligible"] = shs["ok"]
    breadth = setup.get("sector_breadth")
    if isinstance(breadth, Real) and not isinstance(breadth, bool) and math.isfinite(breadth):
        snapshot.update(no_chase=setup["no_chase"], entry_ceiling=setup["entry_ceiling"])
    if ticker in earnings_dates:
        earnings_date = date.fromisoformat(earnings_dates[ticker])
        in_window = session <= earnings_date <= session + timedelta(days=2)
        snapshot.update(earnings_date=earnings_date.isoformat(),
                        earnings=[ticker] if in_window else [])
        if in_window and isinstance(rationale.get("size_ratio"), Real):
            snapshot["earnings_size_factor"] = rationale["size_ratio"]
            snapshot["earnings_size_limit"] = config.EVENT_EARNINGS_SIZE
    return snapshot


def project_journal(previous, state):
    """Rebuild the journal from the committed transition history and old hypotheses."""
    legacy = [{**row, "accounting_basis": "legacy_hypothesis"} for row in previous
              if row.get("accounting_basis") != "paper_execution" or not row.get("signal_id")]
    rows = {}
    for event in state["paper_events"]:
        sid = event["signal_id"]
        signal = state["paper_signals"][sid]
        kind = event["event_type"]
        if kind == "registered":
            if sid in rows:
                raise ValueError("Paper signal was registered twice")
            rows[sid] = {
                "date": signal["signal_session"], "ticker": signal["ticker"],
                "signal_id": sid, "security_id": signal["security_id"],
                "account_currency": "USD", "quote_currency": "USD",
                "accounting_basis": "paper_execution", "planned_open_session": signal["planned_open_session"],
                "mode": "标准做多" if "标准" in signal["mode"] else "轻仓试错",
                **{key: signal[key] for key in ("template", "sector", "chain", "entry_ref",
                    "stop", "tss_final", "tos", "mrs_star", "action", "source",
                    "market_ref", "approval_ref", "decision_ref", "review_snapshot")},
                "status": "pending", "entry": None, "entry_date": None,
                "exit": None, "exit_date": None, "r": None, "win": None,
                "paper_event_ids": {},
            }
        row = rows[sid]
        if kind == "filled":
            if row["status"] != "pending":
                raise ValueError("Paper fill does not follow a pending signal")
            fill = event["fill"]
            row.update(status="open", entry=fill["entry"], entry_raw=fill["entry_raw"],
                       entry_date=event["session"], shares=fill["shares"],
                       entry_friction_cost=fill["friction_cost"], risk_usd=fill["risk_usd"])
        elif kind == "closed":
            if row["status"] != "open":
                raise ValueError("Paper exit does not follow a fill")
            close = event["close"]
            row.update(status="closed", exit=close["exit"], exit_date=event["session"],
                       r=close["r_multiple"], win=close["pnl_usd"] > 0,
                       pnl_usd=close["pnl_usd"], friction_cost=close["friction_cost"],
                       note=event["reason"])
        elif kind in {"expired", "rejected"}:
            if row["status"] != "pending":
                raise ValueError("Paper refusal does not follow a pending signal")
            row.update(status="void", outcome_reason=event["reason"], note=event["reason"])
        row["paper_event_ids"][kind] = event["event_id"]
    for row in rows.values():
        if row["status"] in {"open", "closed"}:
            row["protected"] = "protected" in row["paper_event_ids"]
    for position in state["positions"]:
        sid = position.get("signal_id")
        if sid and position.get("last_bar_date"):
            row = rows[sid]
            row["r_live"] = round((position["last_price"] - position["entry_price"])
                                  * position["shares"] / max(position["risk_usd"], 1e-9), 3)
            row["mark_session"] = position["last_bar_date"]
    return legacy + list(rows.values())
