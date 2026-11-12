"""Bitemporal reviewed US roster and receipt-bound scoped live membership."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from hashlib import sha256
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from .agent_evidence import json_bytes, strict_json

_SCHEMA = "tiger.security-roster.v1"
_SCOPE = "us-reviewed-paper"
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}\Z")
_ID = re.compile(r"[a-z][a-z0-9_\-]*:[A-Za-z0-9_.\-]+\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_ORDER = {kind: i for i, kind in enumerate(("listing", "rename", "split", "halt", "resume", "delist"))}
_DETAILS = {"listing": {"symbol"}, "rename": {"symbol"}, "split": {"ratio"}}


def _check(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"Invalid {label} fields")


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _day(value):
    if not isinstance(value, str):
        raise ValueError("Invalid roster session")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Roster session must be YYYY-MM-DD")
    return parsed


def _instant(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Roster timestamp must include time and offset")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Roster timestamp must include time and offset")
    return parsed.astimezone(timezone.utc)


def _file_bytes(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Roster evidence must be a regular file")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError("Roster evidence size is outside limits")
    return raw


@dataclass(frozen=True)
class Member:
    security_id: str
    symbol: str
    bars_since: date
    status: str


@dataclass(frozen=True)
class RosterView:
    members: dict[str, Member]
    unavailable: dict[str, str]
    source_refs: tuple[str, ...]

    @property
    def symbols(self):
        return {symbol: member.security_id for symbol, member in self.members.items()}


@dataclass(frozen=True)
class RosterEvent:
    security_id: str
    effective_date: date
    known_at: datetime
    kind: str
    source_ref: str
    symbol: str | None


class SecurityRoster:
    def __init__(self, digest, sources, sessions, events):
        self.digest = digest
        self.sources = sources
        self.sessions = sessions
        self.events = events

    @classmethod
    def load(cls, path, expected_sha256):
        """Load an operator-pinned roster and verify each local source artifact."""
        path = Path(path)
        raw = _file_bytes(path, 8 * 1024 * 1024)
        digest = sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise ValueError("Roster packet digest differs")
        packet = strict_json(raw)
        _check(packet, ("schema", "scope", "sources", "sessions", "events"), "roster packet")
        if packet["schema"] != _SCHEMA or packet["scope"] != _SCOPE:
            raise ValueError("Unsupported roster schema or scope")
        sources = packet["sources"]
        sessions = packet["sessions"]
        events = packet["events"]
        if (not isinstance(sources, dict) or not 0 < len(sources) <= 2000
                or not isinstance(sessions, dict) or not sessions
                or not isinstance(events, list) or not events):
            raise ValueError("Roster must contain sources, session coverage and events")
        published = {}
        for ref, source in sources.items():
            _check(source, ("url", "sha256", "published_at"), "source")
            if not isinstance(source["url"], str) or not _matches(_SHA256, source["sha256"]):
                raise ValueError("Invalid roster source address or hash")
            url = urlsplit(source["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Roster source needs a credential-free HTTPS URL")
            published[ref] = _instant(source["published_at"])
            source_bytes = _file_bytes(path.parent / f"{source['sha256']}.bin", 20 * 1024 * 1024)
            if sha256(source_bytes).hexdigest() != source["sha256"]:
                raise ValueError("Roster source digest differs")

        def clock(record):
            ref = record["source_ref"]
            if not isinstance(ref, str) or ref not in published:
                raise ValueError("Unknown roster source")
            instant = _instant(record["known_at"])
            if instant < published[ref]:
                raise ValueError("Roster evidence precedes source publication")
            return instant, ref

        parsed_sessions = {}
        for session, info in sessions.items():
            _check(info, ("known_at", "source_ref"), "roster session")
            parsed_sessions[_day(session)] = clock(info)
        parsed_events = []
        seen = set()
        for entry in events:
            kind = entry.get("kind") if isinstance(entry, dict) else None
            if not isinstance(kind, str) or kind not in _ORDER:
                raise ValueError("Invalid roster event")
            _check(entry, {"kind", "security_id", "effective_date", "known_at", "source_ref"}
                   | _DETAILS.get(kind, set()), "roster event")
            symbol = entry.get("symbol")
            identity = entry["security_id"]
            if not _matches(_ID, identity) or identity.startswith("us:"):
                raise ValueError("Roster events require an externally sourced security identity")
            if kind in ("listing", "rename") and not _matches(_SYMBOL, symbol):
                raise ValueError("Invalid roster event symbol")
            if kind == "split" and (type(entry["ratio"]) not in (int, float)
                                     or not math.isfinite(entry["ratio"])
                                     or entry["ratio"] <= 0 or entry["ratio"] == 1):
                raise ValueError("Invalid split ratio")
            known_at, ref = clock(entry)
            effective = _day(entry["effective_date"])
            key = (identity, effective, kind, known_at)
            if key in seen:
                raise ValueError("Duplicate or contradictory roster event revision")
            seen.add(key)
            parsed_events.append(RosterEvent(identity, effective, known_at, kind, ref, symbol))
        return cls(digest, sources, parsed_sessions, sorted(parsed_events, key=lambda event: (
            event.effective_date, _ORDER[event.kind], event.security_id, event.known_at)))

    def on(self, session, known_at, *, require_coverage=True):
        """Project only changes effective for this session and observable at the cutoff."""
        if known_at.utcoffset() is None:
            raise ValueError("Roster projection requires a timezone-aware cutoff")
        attestation = self.sessions.get(session)
        if attestation and attestation[0] > known_at:
            attestation = None
        if require_coverage and not attestation:
            raise ValueError(f"No dated roster coverage for {session.isoformat()} at the decision cutoff")
        selected = {}
        for event in self.events:
            if event.effective_date > session:
                break
            if event.known_at <= known_at:
                selected[(event.security_id, event.effective_date, event.kind)] = event
        members = {}
        refs = {attestation[1]} if attestation else set()
        for event in selected.values():
            identity = event.security_id
            kind = event.kind
            refs.add(event.source_ref)
            old = members.get(identity)
            if kind == "listing":
                if old is not None:
                    raise ValueError("A listing cannot reuse a security identity")
                members[identity] = Member(identity, event.symbol, event.effective_date, "active")
            elif old is None or old.status == "delisted":
                raise ValueError("Roster action has no prior continuing security identity")
            elif kind == "rename":
                members[identity] = replace(old, symbol=event.symbol, bars_since=event.effective_date)
            elif kind == "split":
                members[identity] = replace(old, bars_since=event.effective_date)
            else:
                if (kind == "halt" and old.status != "active") or (kind == "resume" and old.status != "halted"):
                    raise ValueError("Roster halt or resume contradicts prior state")
                status = {"halt": "halted", "resume": "active", "delist": "delisted"}[kind]
                members[identity] = replace(old, status=status)
        active = {}
        unavailable = {}
        for member in members.values():
            if member.status == "active":
                if member.symbol in active:
                    raise ValueError(f"Ambiguous active roster alias {member.symbol}")
                active[member.symbol] = member
            else:
                unavailable[member.symbol] = member.status
        return RosterView(active, {ticker: status for ticker, status in unavailable.items()
                                   if ticker not in active}, tuple(sorted(refs)))


def observed_roster(universe, session, admitted_at):
    """Describe the reviewed pool observed at admission, with run-scoped alias IDs."""
    if admitted_at.utcoffset() is None or len(set(universe)) != len(universe):
        raise ValueError("Invalid observed roster")
    payload = {
        "contract": _SCHEMA, "basis": "reviewed_pool_observed_now",
        "session": session.isoformat(),
        "known_at": admitted_at.astimezone(timezone.utc).isoformat(),
        "symbols": {ticker: f"us:{ticker}" for ticker in sorted(universe)},
        "source_refs": ["pipeline.prepare:reviewed_us_pool"],
    }
    return {**payload, "sha256": sha256(json_bytes(payload)).hexdigest()}


def verify_observed_roster(run, raw, session, admitted_at):
    """Independently bind the published view to both sealed stage outputs."""
    from .agent_evidence import EvidenceInvalid, read_bytes, read_snapshot

    values = {}
    for line in read_bytes(run, "events.jsonl").splitlines():
        row = strict_json(line)
        stage = row["stage"]
        if stage in ("pipeline.prepare", "data.prepare") and row["kind"] == "end":
            values[stage] = read_snapshot(run, row["output_ref"], run_id=run.name,
                                          stage=stage, role="output")["value"]
    try:
        setup = values["pipeline.prepare"]
        market = values["data.prepare"]
        expected = observed_roster(setup["universe"], session, admitted_at)
        if (market["universe"] != setup["universe"]
                or any(stage.get("security_roster") != expected for stage in (setup, market, raw))):
            raise ValueError("roster differs")
    except (TypeError, KeyError, ValueError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Sealed reviewed security roster differs",
                              stage="data.prepare") from exc
