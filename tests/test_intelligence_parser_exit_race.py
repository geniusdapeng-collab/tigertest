"""Real parser completion and still-live observer failures share the original deadline."""
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import time

import pytest
from pypdf import PdfWriter

from trading_system.intelligence.contracts import IntelligenceError
from trading_system.intelligence.extract import extract_response
from trading_system.intelligence.parse_limits import ParsePolicy
from test_intelligence_parse_limits import pdf, response


def gated_real_worker(monkeypatch, tmp_path, *, release_on_confirmation):
    """Run the real worker, then hold its exit after it writes its real result."""
    from trading_system.intelligence import parse_limits
    original = parse_limits.subprocess.Popen
    ready, release = tmp_path / "worker-ready", tmp_path / "worker-release"
    children, confirmations = [], []
    fault = {"observed": False}
    script = ("import pathlib,runpy,sys,time; "
        "worker=runpy.run_path(sys.argv[1],run_name='parser_exit_test'); "
        "code=worker['main'](); pathlib.Path(sys.argv[2]).touch(); "
        "release=pathlib.Path(sys.argv[3]); "
        "exec('while not release.exists():\\n time.sleep(0.001)'); "
        "raise SystemExit(code)")

    class Child:
        def __init__(self, actual):
            self.actual, self.pid = actual, actual.pid

        @property
        def returncode(self):
            return self.actual.returncode

        def communicate(self, *args, **kwargs):
            if fault["observed"] and kwargs.get("timeout") is not None:
                confirmations.append(kwargs["timeout"])
                if release_on_confirmation:
                    release.touch()
            return self.actual.communicate(*args, **kwargs)

        def poll(self):
            return self.actual.poll()

        def kill(self):
            return self.actual.kill()

    def start(args, **kwargs):
        if not any(str(value).endswith("parse_worker.py") for value in args):
            return original(args, **kwargs)
        worker = Path(args[-1])
        actual = original([sys.executable, "-I", "-B", "-c", script,
            str(worker), str(ready), str(release)], **kwargs)
        child = Child(actual)
        children.append(child)
        return child

    monkeypatch.setattr(parse_limits.subprocess, "Popen", start)
    return ready, fault, children, confirmations


@pytest.mark.parametrize("fault_kind", ["missing", "read_error"])
@pytest.mark.parametrize("pdf_kind", ["encrypted", "malformed"])
def test_actual_pdf_result_survives_confirmed_exit_observer_race(monkeypatch, tmp_path, fault_kind, pdf_kind):
    from trading_system.intelligence import parse_limits
    body = b"%PDF-1.4\nmalformed"
    if pdf_kind == "encrypted":
        writer = PdfWriter(clone_from=BytesIO(pdf(1)))
        writer.encrypt("fixture-password")
        output = BytesIO(); writer.write(output)
        body = output.getvalue()
    ready, fault, children, confirmations = gated_real_worker(monkeypatch, tmp_path,
        release_on_confirmation=True)
    observe = parse_limits._resident_bytes

    def missing_during_exit(pid, remaining):
        if not ready.exists():
            return observe(pid, remaining)
        assert children[0].poll() is None
        fault["observed"] = True
        if fault_kind == "read_error":
            raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE",
                details={"observer_reason": "PROC_STATUS_UNAVAILABLE"})
        return None

    monkeypatch.setattr(parse_limits, "_resident_bytes", missing_during_exit)
    result = extract_response(response(body, "application/pdf"), policy=ParsePolicy(timeout_seconds=3))
    assert fault["observed"], result._parser_runtime
    assert result.metadata["extraction_error"] == "PDF_PARSE_FAILED", result._parser_runtime
    assert result.raw_bytes == body and result.text == "" and result.completeness == "partial"
    assert len(confirmations) == 1 and 0 < confirmations[0] <= 0.05
    assert children[0].poll() == 0 and result._parser_runtime["worker_returncode"] == 0
    reason = "LIVE_PROCESS_NOT_OBSERVED" if fault_kind == "missing" else "PROC_STATUS_UNAVAILABLE"
    assert result._parser_runtime["observer_reason"] == "EXIT_CONFIRMED_AFTER_" + reason
    assert result._parser_runtime["worker_phase"] == "output_ready"


@pytest.mark.parametrize("fault_kind", ["missing", "read_error"])
def test_actual_live_unobservable_worker_is_still_rejected_and_reaped(monkeypatch, tmp_path, fault_kind):
    from trading_system.intelligence import parse_limits
    ready, fault, children, confirmations = gated_real_worker(monkeypatch, tmp_path,
        release_on_confirmation=False)
    observe = parse_limits._resident_bytes

    def unobservable(pid, remaining):
        if not ready.exists():
            return observe(pid, remaining)
        assert children[0].poll() is None
        fault["observed"] = True
        if fault_kind == "read_error":
            raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", details={"observer_reason": "PROC_STATUS_UNAVAILABLE"})
        return None

    monkeypatch.setattr(parse_limits, "_resident_bytes", unobservable)
    result = extract_response(response(b"%PDF-1.4\nmalformed", "application/pdf"),
        policy=ParsePolicy(timeout_seconds=3))
    assert fault["observed"] and result.metadata["extraction_error"] == "PARSER_RESOURCE_UNAVAILABLE"
    assert len(confirmations) == 1 and 0 < confirmations[0] <= 0.05
    assert result._parser_runtime["peak_resident_bytes"] is None or result._parser_runtime["resident_samples"] > 0
    assert result.text == "" and result.completeness == "partial" and children[0].poll() == -9
    assert not (tmp_path / "worker-release").exists()


def test_actual_unobservable_confirmation_cannot_reset_shared_deadline(monkeypatch, tmp_path):
    from trading_system.intelligence import parse_limits
    ready, fault, children, confirmations = gated_real_worker(monkeypatch, tmp_path,
        release_on_confirmation=False)
    monotonic, observe = time.monotonic, parse_limits._resident_bytes
    cutoff = monotonic() + 3
    advanced = {"offset": 0.0}

    def clock():
        return monotonic() + advanced["offset"]

    def unobservable(pid, remaining):
        if not ready.exists():
            return observe(pid, remaining)
        fault["observed"] = True
        advanced["offset"] = cutoff - monotonic() - 0.015
        return None

    monkeypatch.setattr(parse_limits.time, "monotonic", clock)
    monkeypatch.setattr(parse_limits, "_resident_bytes", unobservable)
    result = extract_response(response(b"%PDF-1.4\nmalformed", "application/pdf"),
        policy=ParsePolicy(timeout_seconds=30), deadline=cutoff)
    assert fault["observed"] and result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert len(confirmations) == 1 and 0 < confirmations[0] < 0.02
    assert children[0].poll() == -9 and result.text == "" and result.completeness == "partial"


def test_completion_after_exit_confirmation_cutoff_is_not_accepted(monkeypatch):
    from trading_system.intelligence import parse_limits
    from test_intelligence_parse_limits import controlled_child
    observed, child, clock = controlled_child(monkeypatch)
    original = child.communicate

    def communicate(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except subprocess.TimeoutExpired:
            clock.now = 10.5
            raise

    monkeypatch.setattr(child, "communicate", communicate)
    monkeypatch.setattr(parse_limits, "_resident_bytes", lambda *_args: None)
    result = extract_response(observed, policy=ParsePolicy(timeout_seconds=1))
    assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert child.poll() == 0 and result.text == "" and result.completeness == "partial"
