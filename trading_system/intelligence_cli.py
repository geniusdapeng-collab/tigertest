"""Bounded acquisition-only CLI; trusted deployment options stay outside requests.

There is deliberately no free-form URL, query, path, environment, model, account,
order or permission field.  This command cannot grant evidence scoring/action
eligibility and never executes the paper-account workflow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

from .intelligence.contracts import IntelligenceError, aware, canonical, digest, safe_value
from .intelligence.registry import default_registry
from .universe import EXTENDED_UNIVERSE


OPERATIONS = ("sync", "monitor", "status", "evidence")
REQUEST_SCHEMA = "tiger.intelligence-request/v1"
RESULT_SCHEMA = "tiger.intelligence-operation/v1"
INPUT_BYTES = 65536
OUTPUT_BYTES = 8 * 1024 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_OBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}")
_SOURCE_NAMES = {"web_page", "deepseek", "brave", "tavily", "exa", "sec_directory", "sec_current", "sec_submissions",
                 "fed_policy", "bls_calendar", "fred", "cboe_vix"}


def _invalid():
    raise IntelligenceError("INVALID_REQUEST")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def read_request(stream):
    try:
        raw = stream.read(INPUT_BYTES + 1)
        if len(raw) > INPUT_BYTES:
            raise IntelligenceError("INPUT_TOO_LARGE")
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        request = json.loads(text, object_pairs_hook=_pairs,
                             parse_constant=lambda _: _invalid())
        canonical(request)
        if not isinstance(request, dict):
            _invalid()
        return request
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _invalid()


def validate_request(operation, request, registry, *, now=None, reviewed_symbols=None):
    if operation not in OPERATIONS or not isinstance(request, dict):
        _invalid()
    if operation in {"sync", "monitor"}:
        if set(request) != {"schema", "request_id", "as_of", "symbols", "source_ids"}:
            _invalid()
        if request["schema"] != REQUEST_SCHEMA or not isinstance(request["request_id"], str) or not _REQUEST_ID.fullmatch(request["request_id"]):
            _invalid()
        cutoff = aware(request["as_of"])
        current = now or datetime.now(timezone.utc)
        if cutoff > aware(current):
            raise IntelligenceError("BAD_AS_OF", "Future cutoff is not an acquired observation")
        symbols = request["symbols"]
        sources = request["source_ids"]
        reviewed = set(EXTENDED_UNIVERSE if reviewed_symbols is None else reviewed_symbols)
        if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 100
                or any(not isinstance(x, str) or x not in reviewed for x in symbols)
                or len(set(symbols)) != len(symbols)
                or not isinstance(sources, list) or not 1 <= len(sources) <= len(_SOURCE_NAMES)
                or any(not isinstance(x, str) or x not in registry.source_ids() for x in sources)
                or len(set(sources)) != len(sources)):
            _invalid()
        return {**request, "as_of": cutoff.isoformat(), "symbols": sorted(symbols), "source_ids": sorted(sources)}
    if operation == "status":
        if set(request) != {"job_id"} or not _identity(request["job_id"]):
            _invalid()
        return request
    allowed = {"bundle_id", "document_id", "claim_id"}
    if not request or set(request) - allowed or any(not _identity(x) for x in request.values()):
        _invalid()
    if "claim_id" in request and "bundle_id" not in request or "document_id" in request and "claim_id" in request:
        _invalid()
    return request


def _identity(value):
    return isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None and ".." not in value


def _default_service(root, registry, *, readonly):
    from .intelligence import IntelligenceService
    return IntelligenceService(root, registry, {} if readonly else registry.build_adapters(),
                               tenant="public", readonly=readonly)


def _plan_request(request, cycle):
    symbols = request["symbols"]
    frontier = {"schema": "tiger.intelligence-cli-frontier/v1", "symbols": symbols,
                "as_of": request["as_of"], "basis": "reviewed_selection_no_market_snapshot"}
    return {"run_id": "intel-cli-" + digest({"request": request, "cycle": cycle})[:32],
            "as_of": request["as_of"], "market": "us",
            "universe": {symbol: "listed:US:" + symbol for symbol in symbols},
            "frontier": symbols, "frontier_manifest_hash": digest(frontier),
            "market_snapshot_refs": [], "holdings": None, "watchlist": [],
            "source_ids": request["source_ids"],
            "limits": {"requests": 24, "fetches": 12, "bytes": 8 * 1024 * 1024,
                       "rounds": 1, "concurrency": 1, "deadline_seconds": 60}}


def execute(operation, request, *, archive_root, mode="readonly", enabled_sources=(), cycles=1,
            interval_seconds=30, wall_seconds=300, service_factory=None, plan_builder=None,
            monotonic=time.monotonic, sleep=time.sleep, now=None, reviewed_symbols=None):
    """Injectable boundary for offline tests; production factories are never input fields."""
    if (mode not in {"readonly", "acquire"} or not isinstance(archive_root, (str, Path))
            or not Path(archive_root).is_absolute()
            or any(x not in _SOURCE_NAMES for x in enabled_sources)
            or type(cycles) is not int or not 1 <= cycles <= 12
            or type(interval_seconds) is not int or not 1 <= interval_seconds <= 300
            or type(wall_seconds) is not int or not 60 <= wall_seconds <= 3600):
        raise IntelligenceError("INVALID_STARTUP")
    if operation in {"sync", "monitor"} and mode != "acquire":
        # Reject before opening/creating any archive or resolving credentials.
        raise IntelligenceError("READ_ONLY")
    if operation != "monitor" and cycles != 1:
        raise IntelligenceError("INVALID_STARTUP")
    overrides = {source: {"enabled": True} for source in enabled_sources}
    registry = default_registry({"sources": overrides})
    request = validate_request(operation, request, registry, now=now, reviewed_symbols=reviewed_symbols)
    service = (service_factory or _default_service)(archive_root, registry, readonly=operation in {"status", "evidence"})
    if operation == "status":
        data = service.status(request["job_id"])
    elif operation == "evidence":
        data = service.evidence(**request)
    else:
        if plan_builder is None:
            from .intelligence import build_plan
            plan_builder = build_plan
        deadline = monotonic() + wall_seconds
        results = []
        stopped = None
        for cycle in range(cycles if operation == "monitor" else 1):
            remaining = deadline - monotonic()
            if cycle > 0 and remaining < 60:
                stopped = "WALL_DEADLINE"
                break
            plan_input = _plan_request(request, cycle)
            plan = plan_builder(plan_input, registry)
            replay_key = "cli:" + request["request_id"] + ":" + str(cycle)
            job = service.create_job(plan, replay_key)
            if not job.get("bundle"):
                service.collect(job["job_id"])
                current = service.status(job["job_id"])
                bundle = service.freeze(job["job_id"], expected_plan_hash=plan["plan_hash"],
                    expected_revision=current["revision"], expected_frontier_hash=plan["frontier_manifest_hash"])
                current = service.status(job["job_id"])
                current["bundle"] = {key: bundle[key] for key in ("bundle_id", "manifest_sha256") if key in bundle}
            else:
                current = job
            results.append({"cycle": cycle + 1, **current})
            if cycle + 1 < cycles:
                wait = min(interval_seconds, max(0, deadline - monotonic()))
                if wait < interval_seconds:
                    stopped = "WALL_DEADLINE"
                    break
                sleep(wait)
        data = {"cycles_requested": cycles if operation == "monitor" else 1, "cycles_completed": len(results),
                "stopped_reason": stopped, "cutoff_advanced": False, "cycles": results,
                "semantics": "acquisition_only_unassessed", "account_connected": False}
    return {"schema": RESULT_SCHEMA, "operation": operation, "operation_ok": True, "data": data,
            "error": None, "limitations": ["Acquisition status does not certify evidence eligibility or a completed research workflow.",
                "No text model, account or order is executed by this command.",
                "Monitoring keeps the explicit cutoff; late versions cannot silently become historical evidence."]}


def failure(operation, code):
    return {"schema": RESULT_SCHEMA, "operation": operation if operation in OPERATIONS else None,
            "operation_ok": False, "data": {}, "error": {"code": code}, "limitations": []}


def exit_code(result):
    if result["operation_ok"]:
        return 0
    code = result["error"]["code"]
    return 3 if code in {"READ_ONLY", "ACCESS_DENIED", "POLICY_DENIED"} else 2 if code in {
        "INVALID_REQUEST", "INVALID_STARTUP", "INPUT_TOO_LARGE", "BAD_AS_OF"} else 7


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded registered-source acquisition; no account or order operation")
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--mode", choices=("readonly", "acquire"), default="readonly")
    parser.add_argument("--enable-source", action="append", default=[])
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--wall-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.operation in {"sync", "monitor"} and args.mode != "acquire":
            raise IntelligenceError("READ_ONLY")
        request = read_request(sys.stdin.buffer)
        result = execute(args.operation, request, archive_root=args.archive_root, mode=args.mode,
                         enabled_sources=args.enable_source, cycles=args.cycles,
                         interval_seconds=args.interval_seconds, wall_seconds=args.wall_seconds)
    except IntelligenceError as error:
        result = failure(args.operation, error.code)
    except KeyboardInterrupt:
        result = failure(args.operation, "INTERRUPTED")
    except Exception:
        result = failure(args.operation, "INTERNAL_ERROR")
    secrets = tuple(value for key, value in os.environ.items()
                    if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I) and len(value) >= 4)
    payload = canonical(safe_value(result, secrets))
    if len(payload) > OUTPUT_BYTES:
        result = failure(args.operation, "OUTPUT_TOO_LARGE")
        payload = canonical(result)
    try:
        sys.stdout.buffer.write(payload + b"\n")
    except BrokenPipeError:
        return 130
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
