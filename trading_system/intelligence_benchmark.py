"""Offline, independently labelled acquisition comparisons.

This module never searches, fetches, calls an LLM or turns benchmark labels into
trading permission.  Production captures must come from the bounded acquisition
service.  Frozen retrieval captures and the adjudicated reference set are inputs,
so an evaluator cannot quietly use one provider's result union as its own truth.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


CATEGORIES = ("macro_policy", "earnings_guidance", "industry_supply_chain",
              "negative_risk", "time_entity_repost_traps")
DATASET_SCHEMA = "tiger.intelligence-benchmark-dataset/v1"
CAPTURE_SCHEMA = "tiger.intelligence-benchmark-capture/v1"
REPORT_SCHEMA = "tiger.intelligence-benchmark-report/v1"


class BenchmarkError(ValueError):
    """Public errors contain a fixed diagnostic, never arbitrary input content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise BenchmarkError("INVALID_JSON_VALUE") from None


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def load_json(path: Path | str, *, max_bytes: int = 64 * 1024 * 1024):
    """Read one bounded JSON file; reject duplicate keys and non-finite values."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise BenchmarkError("INPUT_TOO_LARGE")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BenchmarkError("NONFINITE_JSON")))
        canonical(value)
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise BenchmarkError("INPUT_UNREADABLE") from None


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _url(value):
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"https", "http"} or not parts.hostname
                or parts.username or parts.password or len(value) > 8192):
            raise ValueError
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                           urlencode(query), ""))
    except (ValueError, AttributeError, TypeError):
        raise BenchmarkError("INVALID_REFERENCE_URL") from None


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise BenchmarkError("INVALID_CONTRACT_FIELDS")


def _strings(value, *, empty=True):
    if (not isinstance(value, list) or not empty and not value
            or any(not isinstance(x, str) or not x or len(x) > 512 for x in value)
            or len(set(value)) != len(value)):
        raise BenchmarkError("INVALID_STRING_LIST")
    return value


def validate_dataset(dataset):
    _keys(dataset, ("schema", "dataset_id", "truth_kind", "description", "cases"),
          ("adjudication", "frozen_at"))
    if (dataset["schema"] != DATASET_SCHEMA or dataset["truth_kind"] not in {"synthetic_fixture", "reviewed_gold"}
            or not isinstance(dataset["dataset_id"], str) or not dataset["dataset_id"]
            or not isinstance(dataset["description"], str)
            or not isinstance(dataset["cases"], list) or not dataset["cases"]):
        raise BenchmarkError("INVALID_DATASET")
    if dataset["truth_kind"] == "reviewed_gold":
        review = dataset.get("adjudication")
        _keys(review, ("reviewer", "reviewed_at", "method", "independent_of_captures"))
        if (not all(isinstance(review[x], str) and review[x].strip() for x in ("reviewer", "method"))
                or _time(review["reviewed_at"]) is None or review["independent_of_captures"] is not True):
            raise BenchmarkError("UNREVIEWED_GOLD")
    case_ids = set()
    for case in dataset["cases"]:
        _keys(case, ("id", "category", "split", "question", "as_of", "expected"),
              ("forbidden_urls", "notes", "allowed_sources"))
        if (not isinstance(case["id"], str) or not case["id"] or case["id"] in case_ids
                or case["category"] not in CATEGORIES or case["split"] not in {"calibration", "blind"}
                or not isinstance(case["question"], str) or not case["question"].strip()
                or _time(case["as_of"]) is None or not isinstance(case["expected"], list)):
            raise BenchmarkError("INVALID_CASE")
        case_ids.add(case["id"])
        event_ids = set()
        for event in case["expected"]:
            _keys(event, ("event_id", "subject_id", "reference_urls", "primary_urls"),
                  ("immutable_versions",))
            if (not isinstance(event["event_id"], str) or not event["event_id"] or event["event_id"] in event_ids
                    or not isinstance(event["subject_id"], str) or not event["subject_id"]):
                raise BenchmarkError("INVALID_EXPECTED_EVENT")
            event_ids.add(event["event_id"])
            refs = {_url(x) for x in _strings(event["reference_urls"], empty=False)}
            primary = {_url(x) for x in _strings(event["primary_urls"])}
            if not primary <= refs:
                raise BenchmarkError("PRIMARY_NOT_IN_REFERENCE_SET")
            if not isinstance(event.get("immutable_versions", []), list):
                raise BenchmarkError("INVALID_IMMUTABLE_SEED")
            for proof in event.get("immutable_versions", []):
                _keys(proof, ("url", "content_sha256", "available_at", "proof_ref"))
                if (_url(proof["url"]) not in refs or _time(proof["available_at"]) is None
                        or not _sha(proof["content_sha256"]) or not isinstance(proof["proof_ref"], str)
                        or not proof["proof_ref"].strip()):
                    raise BenchmarkError("INVALID_IMMUTABLE_SEED")
        for url in _strings(case.get("forbidden_urls", [])):
            _url(url)
        _strings(case.get("allowed_sources", []))
    canonical(dataset)
    return dataset


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(x in "0123456789abcdef" for x in value)


def validate_capture(capture, dataset):
    _keys(capture, ("schema", "dataset_sha256", "provider", "mode", "cases"),
          ("configuration", "created_at", "source_receipts", "notes"))
    if (capture["schema"] != CAPTURE_SCHEMA or capture["dataset_sha256"] != digest(dataset)
            or capture["mode"] not in {"fixture_replay", "frozen_replay", "historical_reconstruction", "prospective"}
            or not isinstance(capture["provider"], str) or not capture["provider"].strip()
            or not isinstance(capture["cases"], list)):
        raise BenchmarkError("INVALID_CAPTURE")
    if dataset["truth_kind"] == "synthetic_fixture" and capture["mode"] != "fixture_replay":
        raise BenchmarkError("FIXTURE_CANNOT_PROVE_PRODUCTION")
    known = {x["id"] for x in dataset["cases"]}
    seen = set()
    for row in capture["cases"]:
        _keys(row, ("case_id", "status", "documents"),
              ("latency_ms", "usage", "cost", "attempt_refs", "errors", "captured_at"))
        if (not isinstance(row["case_id"], str) or row["case_id"] not in known or row["case_id"] in seen
                or row["status"] not in {"completed", "partial", "failed", "blocked"}
                or not isinstance(row["documents"], list)):
            raise BenchmarkError("INVALID_CAPTURE_CASE")
        seen.add(row["case_id"])
        if "latency_ms" in row and not _number(row["latency_ms"]):
            raise BenchmarkError("INVALID_LATENCY")
        if "cost" in row:
            _keys(row["cost"], ("amount", "currency", "state"))
            if (row["cost"]["state"] not in {"reported", "unknown"}
                    or row["cost"]["state"] == "reported" and not _number(row["cost"]["amount"])
                    or not isinstance(row["cost"]["currency"], str)):
                raise BenchmarkError("INVALID_COST")
        for doc in row["documents"]:
            _keys(doc, ("url", "publisher_origin", "event_ids", "subject_ids", "label_status"),
                  ("content", "content_sha256", "evidence_quote", "published_at", "first_seen_at",
                   "fetched_at", "source_id", "document_version", "receipt_ref", "evidence_start", "evidence_end"))
            _url(doc["url"])
            if (not isinstance(doc["publisher_origin"], str) or not doc["publisher_origin"].strip()
                    or doc["label_status"] not in {"fixture_verified", "reviewed", "unreviewed"}):
                raise BenchmarkError("INVALID_CAPTURE_DOCUMENT")
            _strings(doc["event_ids"])
            _strings(doc["subject_ids"])
            if "content" in doc and not isinstance(doc["content"], str):
                raise BenchmarkError("INVALID_DOCUMENT_BODY")
            if "content_sha256" in doc and not _sha(doc["content_sha256"]):
                raise BenchmarkError("INVALID_DOCUMENT_HASH")
            if "evidence_quote" in doc and not isinstance(doc["evidence_quote"], str):
                raise BenchmarkError("INVALID_EVIDENCE_QUOTE")
    canonical(capture)
    return capture


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None,
            "state": "measured" if denominator else "unverified"}


def _quantile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _body_valid(doc):
    content = doc.get("content")
    quote = doc.get("evidence_quote")
    if not (isinstance(content, str) and bool(content) and isinstance(quote, str) and bool(quote)
            and doc.get("content_sha256") == hashlib.sha256(content.encode("utf-8")).hexdigest()):
        return False
    if "evidence_start" in doc or "evidence_end" in doc:
        start, end = doc.get("evidence_start"), doc.get("evidence_end")
        return type(start) is int and type(end) is int and 0 <= start < end <= len(content) and content[start:end] == quote
    return content.count(quote) == 1


def _available(doc, event, cutoff):
    """An old publication label alone cannot date a late-fetched mutable version."""
    published, seen = _time(doc.get("published_at")), _time(doc.get("first_seen_at"))
    if published is not None and published > cutoff or seen is not None and seen > cutoff:
        # A reviewed immutable version can establish earlier availability despite
        # late capture, but cannot erase a future original publication timestamp.
        if published is not None and published > cutoff:
            return False
    if published is not None and published <= cutoff and seen is not None and seen <= cutoff:
        return True
    return any(_url(seed["url"]) == _url(doc["url"])
               and seed["content_sha256"] == doc.get("content_sha256")
               and _time(seed["available_at"]) <= cutoff
               for seed in event.get("immutable_versions", []))


def _case_result(case, row, truth_kind):
    cutoff = _time(case["as_of"])
    docs = row["documents"] if row else []
    accepted_labels = {"fixture_verified"} if truth_kind == "synthetic_fixture" else {"reviewed"}
    refs_found, events_found, primary_found = set(), set(), set()
    future_count, undated_count, identity_bad, label_unknown, source_disallowed = 0, 0, 0, 0, 0
    origins = Counter(x["publisher_origin"] for x in docs)
    expected_refs = {_url(url) for e in case["expected"] for url in e["reference_urls"]}
    forbidden = {_url(url) for url in case.get("forbidden_urls", [])}
    disallowed_sources = set(case.get("allowed_sources", []))
    for doc in docs:
        url = _url(doc["url"])
        if url in expected_refs:
            refs_found.add(url)
        published, seen = _time(doc.get("published_at")), _time(doc.get("first_seen_at"))
        future_count += int(published is not None and published > cutoff)
        undated_count += int(published is None)
        labelled = doc["label_status"] in accepted_labels
        label_unknown += int(not labelled)
        source_ok = not disallowed_sources or doc.get("source_id") in disallowed_sources
        source_disallowed += int(not source_ok)
        reference_seen, identity_matched = False, False
        for event in case["expected"]:
            refs = {_url(x) for x in event["reference_urls"]}
            if url not in refs:
                continue
            reference_seen = True
            identity_ok = event["event_id"] in doc["event_ids"] and event["subject_id"] in doc["subject_ids"]
            identity_matched = identity_matched or identity_ok
            if labelled and identity_ok and source_ok and _available(doc, event, cutoff):
                events_found.add(event["event_id"])
                if url in {_url(x) for x in event["primary_urls"]} and _body_valid(doc):
                    primary_found.add(event["event_id"])
        identity_bad += int(labelled and reference_seen and not identity_matched)
    return {"case_id": case["id"], "category": case["category"], "split": case["split"],
            "capture_status": row["status"] if row else "not_run", "documents": len(docs),
            "expected_events": len(case["expected"]), "reference_urls": len(expected_refs),
            "reference_urls_found": len(refs_found), "qualified_events_found": len(events_found),
            "primary_events_found": len(primary_found), "future_publication_documents": future_count,
            "unknown_publication_documents": undated_count, "wrong_identity_reference_documents": identity_bad,
            "unreviewed_documents": label_unknown, "disallowed_source_documents": source_disallowed,
            "forbidden_documents": sum(_url(x["url"]) in forbidden for x in docs),
            "duplicate_origin_documents": sum(max(0, count - 1) for count in origins.values()),
            "latency_ms": row.get("latency_ms") if row else None,
            "cost": row.get("cost", {"state": "unknown", "amount": None, "currency": "unknown"}) if row else
                    {"state": "unknown", "amount": None, "currency": "unknown"}}


def _aggregate(rows):
    sums = {key: sum(x[key] for x in rows) for key in (
        "documents", "expected_events", "reference_urls", "reference_urls_found", "qualified_events_found",
        "primary_events_found", "future_publication_documents", "unknown_publication_documents",
        "wrong_identity_reference_documents", "unreviewed_documents", "disallowed_source_documents",
        "forbidden_documents", "duplicate_origin_documents")}
    latencies = [x["latency_ms"] for x in rows if x["latency_ms"] is not None]
    reported = [x["cost"] for x in rows if x["cost"]["state"] == "reported"]
    currencies = {x["currency"] for x in reported}
    cost_known = len(reported) == len(rows) and bool(rows) and len(currencies) == 1
    return {"cases": len(rows), "capture_status": dict(Counter(x["capture_status"] for x in rows)), **sums,
            "case_attempt_coverage": _ratio(sum(x["capture_status"] != "not_run" for x in rows), len(rows)),
            "reference_url_recall": _ratio(sums["reference_urls_found"], sums["reference_urls"]),
            "qualified_event_recall": _ratio(sums["qualified_events_found"], sums["expected_events"]),
            "primary_evidence_recall": _ratio(sums["primary_events_found"], sums["expected_events"]),
            "duplicate_origin_rate": _ratio(sums["duplicate_origin_documents"], sums["documents"]),
            "future_publication_rate": _ratio(sums["future_publication_documents"], sums["documents"]),
            "latency_ms": {"samples": len(latencies), "missing": len(rows) - len(latencies),
                           "p50": _quantile(latencies, .5), "p95": _quantile(latencies, .95)},
            "cost": {"state": "reported" if cost_known else "unverified", "cases_reported": len(reported),
                     "cases_unknown": len(rows) - len(reported),
                     "amount": sum(x["amount"] for x in reported) if cost_known else None,
                     "currency": next(iter(currencies)) if cost_known else None}}


def evaluate_capture(dataset, capture):
    """Return deterministic retrieval metrics with every missing case in scope."""
    validate_dataset(dataset)
    validate_capture(capture, dataset)
    captured = {x["case_id"]: x for x in capture["cases"]}
    rows = [_case_result(case, captured.get(case["id"]), dataset["truth_kind"]) for case in dataset["cases"]]
    strata = {}
    for field in ("category", "split"):
        strata[field] = {key: _aggregate([x for x in rows if x[field] == key]) for key in sorted({x[field] for x in rows})}
    return {"schema": REPORT_SCHEMA, "dataset_id": dataset["dataset_id"], "dataset_sha256": digest(dataset),
            "capture_sha256": digest(capture), "provider": capture["provider"], "mode": capture["mode"],
            "truth_kind": dataset["truth_kind"],
            "quality_conclusion": "fixture_only" if dataset["truth_kind"] == "synthetic_fixture" else "measured_not_certified",
            "limitations": ["Exact reference URL recall is not exhaustive web recall.",
                            "Qualified event recall requires independently reviewed event and subject labels.",
                            "Publication labels cannot establish a late-fetched mutable version existed before cutoff.",
                            "No trading, model-quality, production SLA or statistical superiority certificate is issued.",
                            "Missing/failed cases remain in denominators; unknown billing is not zero cost."],
            "overall": _aggregate(rows), "strata": strata, "cases": rows}


def compare_captures(dataset, captures):
    reports = [evaluate_capture(dataset, value) for value in captures]
    names = [x["provider"] for x in reports]
    if len(set(names)) != len(names):
        raise BenchmarkError("DUPLICATE_PROVIDER")
    modes = {x["mode"] for x in reports}
    return {"schema": "tiger.intelligence-benchmark-comparison/v1", "dataset_sha256": digest(dataset),
            "comparable_capture_modes": len(modes) <= 1, "reports": reports,
            "conclusion": "fixture_only" if dataset["truth_kind"] == "synthetic_fixture" else "descriptive_only",
            "reason": "Frozen replay, prospective capture and historical reconstruction are reported separately; no automatic winner."}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Offline frozen-capture acquisition benchmark; performs no network I/O")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--capture", action="append", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        result = compare_captures(load_json(args.dataset), [load_json(x) for x in args.capture])
        payload = canonical(result) + b"\n"
        if args.out:
            with Path(args.out).open("xb") as stream:
                stream.write(payload)
        else:
            print(payload.decode("utf-8"), end="")
        return 0
    except BenchmarkError as error:
        print(json.dumps({"ok": False, "error": error.code}))
        return 2
    except OSError:
        print(json.dumps({"ok": False, "error": "OUTPUT_UNWRITABLE"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
