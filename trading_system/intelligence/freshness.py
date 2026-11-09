"""Frozen material freshness, separate from retrieval and source availability.

Original text remains in the archive. This gate only decides whether a cited
original may fill the requested time coverage cell; it never supplies a fact,
changes a publication timestamp, or claims that a release is the latest one.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re

from .catalog import DIMENSIONS
from .contracts import IntelligenceError, aware, timestamp


def material_freshness(document, dimension, horizon, as_of):
    """Check a complete publication interval against one fixed research cutoff.

    Date-only and unzoned local dates retain their original precision. A wide
    UTC interval is used solely for conservative comparisons; it is not an
    invented publication instant or source timezone. Retrieval/Last-Modified
    never stand in for publication and never make an old opinion current.
    """
    if dimension not in DIMENSIONS or horizon not in {"current", "week", "month", "baseline"}:
        raise IntelligenceError("CONTRACT_INVALID", "Unknown material freshness scope")
    cutoff = aware(as_of)
    policy = DIMENSIONS[dimension]
    days = policy["window_days"] if horizon == "current" else policy["baseline_days"] if horizon == "baseline" else 7 if horizon == "week" else 30
    time_use = "historical_baseline" if horizon == "baseline" else "requested_publication_window"
    # A current broad question can use a reviewed statutory financial report
    # as financial background. Explicit week/month questions remain literal
    # publication windows. This never certifies that a report is the latest.
    metadata = document.get("source_metadata", {}) if isinstance(document, dict) else {}
    authority = document.get("authority", {}) if isinstance(document, dict) else {}
    form = metadata.get("form", "") if isinstance(metadata, dict) else ""
    if (horizon == "current" and dimension == "issuer_disclosure"
            and isinstance(form, str) and form.removesuffix("/A") in {"10-K", "10-Q", "20-F"}
            and isinstance(authority, dict) and authority.get("scope") == "sec_filing"
            and authority.get("proof_type") in {"sec_submission_acceptance", "immutable_accession"}):
        days, time_use = policy["baseline_days"], "financial_statement_baseline"
    start = cutoff - timedelta(days=days)
    published = document.get("times", {}).get("published", {}) if isinstance(document, dict) else {}
    if not isinstance(published, dict):
        published = {}
    precision, raw = published.get("precision", "unknown"), published.get("raw")
    result = {"qualified": False, "reason": "MATERIAL_PUBLICATION_TIME_UNVERIFIED",
        "dimension_id": dimension, "horizon": horizon, "window_days": days, "time_use": time_use,
        "checked_as_of": timestamp(cutoff), "publication_precision": precision,
        "publication_raw": raw, "date_basis": "unknown", "comparison_interval": None,
        "comparison_scope": "publication_window_only_not_latest_release_or_action_eligibility"}
    lower = upper = None
    try:
        if precision == "instant" and published.get("value"):
            lower = upper = aware(published["value"])
            result["date_basis"] = "source_publication_instant"
        else:
            local_day = None
            if precision == "date" and isinstance(published.get("value"), str):
                local_day = date.fromisoformat(published["value"])
                if local_day.isoformat() != published["value"]:
                    raise ValueError
                result["date_basis"] = "source_calendar_date_without_invented_timezone"
            elif precision == "unknown" and isinstance(raw, str) and re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", raw):
                parsed = datetime.fromisoformat(raw)
                if parsed.utcoffset() is None:
                    local_day = parsed.date()
                    result["date_basis"] = "source_local_date_only_publication_instant_unknown"
            if local_day is not None:
                day = datetime.combine(local_day, time(), timezone.utc)
                lower, upper = day - timedelta(days=1), day + timedelta(days=2)
        if lower is None or upper is None:
            return result
        result["comparison_interval"] = {"earliest_utc_bound": timestamp(lower),
            "latest_utc_bound": timestamp(upper), "bounds_are_not_source_timestamps": True}
        if lower > cutoff:
            result["reason"] = "MATERIAL_PUBLICATION_AFTER_CUTOFF"
        elif upper > cutoff:
            result["reason"] = "MATERIAL_PUBLICATION_CUTOFF_UNVERIFIED"
        elif upper < start:
            result["reason"] = "MATERIAL_OUTSIDE_REQUESTED_TIME_WINDOW"
        elif lower < start:
            result["reason"] = "MATERIAL_TIME_WINDOW_BOUNDARY_UNVERIFIED"
        else:
            result.update(qualified=True, reason="MATERIAL_FINANCIAL_BASELINE_WINDOW_QUALIFIED" if time_use == "financial_statement_baseline" else "MATERIAL_BASELINE_WINDOW_QUALIFIED" if horizon == "baseline" else "MATERIAL_REQUESTED_TIME_WINDOW_QUALIFIED")
        return result
    except (ValueError, TypeError, OverflowError, IntelligenceError):
        result.update(qualified=False, reason="MATERIAL_PUBLICATION_TIME_UNVERIFIED", comparison_interval=None)
        return result
