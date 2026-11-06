"""Private byte-only extraction worker. The parent owns its lifetime."""
from __future__ import annotations

if not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import asdict
import json
import math
import os
import resource
import sys

from trading_system.intelligence.contracts import canonical
from trading_system.intelligence.parse_limits import ParsePolicy
from trading_system.intelligence.transport import HttpResponse


def _phase(name, descriptor):
    # Fixed execution markers only: source text, URLs and exception bodies do
    # not enter this channel. The parent retains only the last known marker.
    os.write(descriptor, b"tiger-parser-phase:" + name.encode("ascii") + b"\n")


def _run(phase_descriptor):
    try:
        _phase("started", phase_descriptor)
        line = sys.stdin.buffer.readline(65537)
        if not line.endswith(b"\n") or len(line) > 65536:
            raise ValueError
        header = json.loads(line)
        policy = ParsePolicy.from_value(header["policy"])
        _phase("policy_ready", phase_descriptor)
        resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(policy.timeout_seconds) + 1,
                                               math.ceil(policy.timeout_seconds) + 2))
        # Linux enforces virtual memory in-kernel. Darwin's mapped shared
        # region prevents a useful RLIMIT_AS here; the parent instead samples
        # and enforces resident memory while retaining a hard wall deadline.
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (policy.memory_bytes, policy.memory_bytes))
        elif sys.platform != "darwin":
            raise OSError("Unsupported parser resource platform")
        _phase("limits_ready", phase_descriptor)
        body = sys.stdin.buffer.read(policy.max_input_bytes + 1)
        if len(body) != header["body_bytes"] or len(body) > policy.max_input_bytes:
            raise ValueError
        _phase("input_read", phase_descriptor)
        from trading_system.intelligence.extract import _extract_response_inner
        response = HttpResponse(header["url"], header["status"], header["headers"], body,
            header["received_at"], header["transport_sha256"], header["request_count"])
        result = _extract_response_inner(response, original_url=header["original_url"],
            trusted_filing=header["trusted_filing"], max_pages=policy.max_pages, max_text_bytes=policy.max_text_bytes)
        _phase("builtin_complete", phase_descriptor)
        if result.metadata.get("extraction_error") in {"PARSER_PAGE_LIMIT", "PARSER_TEXT_LIMIT"}:
            value = {"error": result.metadata["extraction_error"]}
        else:
            if policy.html_engine == "scrapling" and result.text_kind in {"html_main", "html_text"}:
                from trading_system.intelligence.scrapling_parser import apply_scrapling
                _phase("scrapling_started", phase_descriptor)
                result = apply_scrapling(result, policy)
                _phase("scrapling_complete", phase_descriptor)
            value = {k: v for k, v in asdict(result).items() if k != "raw_bytes"}
            if len(result.text.encode()) > policy.max_text_bytes:
                value = {"error": "PARSER_TEXT_LIMIT"}
    except MemoryError:
        value = {"error": "PARSER_MEMORY_EXCEEDED"}
    except ImportError:
        value = {"error": "PARSER_DEPENDENCY_UNAVAILABLE"}
    except BaseException:
        value = {"error": "PARSER_FAILED"}
    try:
        _phase("output_ready", phase_descriptor)
        sys.stdout.buffer.write(canonical(value))
        sys.stdout.buffer.flush()
    except (OSError, MemoryError):
        return 1
    return 0


def main():
    # Reserve the inherited stderr pipe for a finite set of fixed markers.
    # Parser/library warnings, including native writes to fd 2, still go to
    # /dev/null so hostile source warnings cannot grow the parent's buffers.
    phase_descriptor = os.dup(2)
    try:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 2)
        return _run(phase_descriptor)
    finally:
        os.dup2(phase_descriptor, 2)
        os.close(phase_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
