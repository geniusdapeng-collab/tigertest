"""Real parser subprocess boundaries, with no network or model calls."""
from dataclasses import asdict, replace
import hashlib
from io import BytesIO
import time
import subprocess
import sys

import pytest
from pypdf import PdfWriter

from trading_system.intelligence.contracts import IntelligenceError, canonical
from trading_system.intelligence.extract import extract_response
from trading_system.intelligence.parse_limits import ParsePolicy
from trading_system.intelligence.transport import HttpResponse


def response(body, media="text/html"):
    return HttpResponse("https://issuer.example/report", 200, {"content-type": media}, body,
        "2026-11-06T00:00:00+00:00", hashlib.sha256(body).hexdigest())


def pdf(pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_page_guard_preserves_original_without_partial_text():
    observed = response(pdf(3), "application/pdf")
    result = extract_response(observed, policy=ParsePolicy(max_pages=2))
    assert result.raw_bytes == observed.body
    assert result.text == ""
    assert result.completeness == "partial"
    assert result.metadata["extraction_error"] == "PARSER_PAGE_LIMIT"


def test_expired_shared_deadline_blocks_parser(monkeypatch):
    from trading_system.intelligence import parse_limits
    def unexpected_child(*_args, **_kwargs):
        pytest.fail("An expired shared deadline must not spawn a parser")
    monkeypatch.setattr(parse_limits.subprocess, "Popen", unexpected_child)
    observed = response(pdf(1), "application/pdf")
    result = extract_response(observed, deadline=time.monotonic() - 1)
    assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert result.text == "" and result.completeness == "partial"
    assert result.raw_bytes == observed.body and result._parser_runtime["worker_returncode"] is None


def test_actual_parser_timeout_reaps_child(monkeypatch):
    from trading_system.intelligence import parse_limits
    original = parse_limits.subprocess.Popen
    children = []
    def start(args, **kwargs):
        if not any(str(value).endswith("parse_worker.py") for value in args):
            return original(args, **kwargs)
        assert set(kwargs["env"]) == {"PATH", "LC_ALL", "PYTHONDONTWRITEBYTECODE"}
        # A real child reads the private input, announces readiness on both
        # pipes, then stays alive beyond the real absolute parser deadline.
        child = original([sys.executable, "-I", "-c",
            "import os,sys,time; assert 'LLM_API_KEY' not in os.environ; "
            "sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'TEST_FIXTURE_PARSER_READY'); sys.stdout.buffer.flush(); "
            "sys.stderr.buffer.write(b'TEST_FIXTURE_PARSER_WAITING'); sys.stderr.buffer.flush(); "
            "time.sleep(60)"], **kwargs)
        observed = {"child": child, "kill_calls": 0, "timeout_calls": 0, "drained": None}
        children.append(observed)
        original_kill, original_communicate = child.kill, child.communicate
        def kill():
            observed["kill_calls"] += 1
            return original_kill()
        def communicate(*args, **kwargs):
            try:
                output = original_communicate(*args, **kwargs)
            except subprocess.TimeoutExpired:
                observed["timeout_calls"] += 1
                raise
            observed["drained"] = output
            return output
        monkeypatch.setattr(child, "kill", kill)
        monkeypatch.setattr(child, "communicate", communicate)
        return child
    monkeypatch.setattr(parse_limits.subprocess, "Popen", start)
    monkeypatch.setenv("LLM_API_KEY", "test-parent-credential")
    observed = response(pdf(1), "application/pdf")
    timeout_seconds = 1.0  # Private test budget permits spawn; production policy is unchanged.
    try:
        started = time.monotonic()
        result = extract_response(observed, policy=ParsePolicy(timeout_seconds=timeout_seconds))
        assert timeout_seconds <= time.monotonic() - started < 3
        assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
        assert result.text == "" and result.completeness == "partial" and result.raw_bytes == observed.body
        assert len(children) == 1
        recorded, child = children[0], children[0]["child"]
        assert recorded["timeout_calls"] >= 1 and recorded["kill_calls"] == 1
        assert recorded["drained"] == (b"TEST_FIXTURE_PARSER_READY", b"TEST_FIXTURE_PARSER_WAITING")
        assert child.poll() is not None and child.returncode < 0
        assert child.stdout.closed and child.stderr.closed
    finally:
        for recorded in children:
            child = recorded["child"]
            if child.poll() is None:
                child.kill()
                child.communicate()


def test_scrapling_only_parses_existing_bytes_and_preserves_table():
    body = (b"<html><title>NVIDIA results</title><nav>bad navigation</nav><article>"
            b"<h1>NVIDIA results</h1><p>Revenue increased.</p><table><tr><th>Period</th>"
            b"<th>Revenue</th></tr><tr><td>Q3</td><td>100</td></tr></table></article></html>")
    result = extract_response(response(body), policy=ParsePolicy(html_engine="scrapling", expected_terms=("NVIDIA",)))
    assert result.raw_bytes == body
    assert result.completeness == "complete"
    assert "bad navigation" not in result.text
    assert "Q3" in result.text and "100" in result.text and " | " in result.text
    assert result.metadata["parser"]["version"] == "0.5.1"
    assert result.metadata["selector_validation"]["validated"] is True


def test_scrapling_selector_drift_is_partial():
    observed = response(b"<title>NVIDIA results</title><div class='sidebar'>NVIDIA buy now</div>")
    result = extract_response(observed, policy=ParsePolicy(html_engine="scrapling", selector=".sidebar", expected_terms=("NVIDIA",)))
    assert result.completeness == "partial"
    assert result.metadata["extraction_error"] == "PARSER_SELECTOR_UNVERIFIED"


def test_parser_text_boundary_does_not_silently_trim():
    observed = response(b"<title>Company</title><article><h1>Company</h1><p>Complete long original text.</p></article>")
    result = extract_response(observed, policy=ParsePolicy(html_engine="scrapling", max_text_bytes=8))
    assert result.text == "" and result.raw_bytes == observed.body
    assert result.metadata["extraction_error"] == "PARSER_TEXT_LIMIT"


@pytest.mark.parametrize("values", [{"timeout_seconds": True}, {"memory_bytes": 1},
    {"max_pages": 0}, {"html_engine": "stealth"}, {"expected_terms": "text"}, {"command": "anything"}])
def test_parser_policy_is_typed_trusted_configuration(values):
    with pytest.raises(IntelligenceError):
        ParsePolicy.from_value(values)


class ParserClock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


def test_parser_preparation_deadline_expires_without_spawning(monkeypatch):
    from trading_system.intelligence import parse_limits
    clock, inputs = ParserClock(), []
    temporary_file = parse_limits.tempfile.TemporaryFile
    def prepare(*args, **kwargs):
        private_input = temporary_file(*args, **kwargs)
        inputs.append(private_input)
        clock.now += 0.001
        return private_input
    def unexpected_child(*_args, **_kwargs):
        pytest.fail("A deadline exhausted during input preparation must not spawn a parser")
    monkeypatch.setattr(parse_limits.time, "monotonic", clock)
    monkeypatch.setattr(parse_limits.tempfile, "TemporaryFile", prepare)
    monkeypatch.setattr(parse_limits.subprocess, "Popen", unexpected_child)
    observed = response(pdf(1), "application/pdf")
    result = extract_response(observed, policy=ParsePolicy(timeout_seconds=0.001))
    assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert result.text == "" and result.completeness == "partial" and result.raw_bytes == observed.body
    assert result._parser_runtime["worker_returncode"] is None
    assert inputs and all(private_input.closed for private_input in inputs)


def controlled_child(monkeypatch, *, finish_late=False, timeout_seconds=1):
    from trading_system.intelligence import parse_limits
    observed = response(b"<title>Original</title><article><h1>Original</h1><p>Complete source body.</p></article>")
    original = extract_response(observed)
    output = canonical({key: value for key, value in asdict(original).items() if key != "raw_bytes"})
    clock = ParserClock()
    class Child:
        pid = 12345
        returncode = None
        calls = 0
        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.returncode is not None:
                return output, b""
            if self.calls == 1 and not finish_late:
                clock.now = 10.0 + timeout_seconds
                raise subprocess.TimeoutExpired("test-fixture-parser", timeout)
            clock.now = 10.0 + timeout_seconds
            self.returncode = 0
            return output, b""
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -9
    child = Child()
    monkeypatch.setattr(parse_limits.time, "monotonic", clock)
    monkeypatch.setattr(parse_limits.subprocess, "Popen", lambda *args, **kwargs: child)
    return observed, child, clock


@pytest.mark.parametrize("timeout_seconds", [0.001, 1])
def test_expired_communication_does_not_launch_a_memory_observer(monkeypatch, timeout_seconds):
    from trading_system.intelligence import parse_limits
    observed, child, _ = controlled_child(monkeypatch, timeout_seconds=timeout_seconds)
    probes = []
    monkeypatch.setattr(parse_limits, "_resident_bytes", lambda *args: probes.append(args) or None)
    result = extract_response(observed, policy=ParsePolicy(timeout_seconds=timeout_seconds))
    assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert result.text == "" and result.completeness == "partial" and child.poll() is not None
    assert probes == []


@pytest.mark.parametrize("timeout_seconds", [0.001, 1])
def test_parser_completion_after_cutoff_cannot_become_complete_evidence(monkeypatch, timeout_seconds):
    observed, child, _ = controlled_child(monkeypatch, finish_late=True, timeout_seconds=timeout_seconds)
    result = extract_response(observed, policy=ParsePolicy(timeout_seconds=timeout_seconds))
    assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert result.text == "" and result.completeness == "partial" and child.poll() is not None


@pytest.mark.parametrize("timeout_seconds", [0.001, 1])
def test_observer_failure_at_cutoff_is_deadline_not_unavailable(monkeypatch, timeout_seconds):
    from trading_system.intelligence import parse_limits
    observed, child, clock = controlled_child(monkeypatch, timeout_seconds=timeout_seconds)
    def observe(*_args):
        clock.now = 10.0 + timeout_seconds
        raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE")
    original_communicate = child.communicate
    def communicate(*args, **kwargs):
        try:
            return original_communicate(*args, **kwargs)
        except subprocess.TimeoutExpired:
            clock.now = 10.0 + timeout_seconds / 2
            raise
    monkeypatch.setattr(child, "communicate", communicate)
    monkeypatch.setattr(parse_limits, "_resident_bytes", observe)
    result = extract_response(observed, policy=ParsePolicy(timeout_seconds=timeout_seconds))
    assert result.metadata["extraction_error"] == "PARSER_DEADLINE_EXCEEDED"
    assert child.poll() is not None


@pytest.mark.parametrize("failed_observer", ["missing", "timeout"])
def test_live_parser_without_observable_memory_fails_closed(monkeypatch, failed_observer):
    from trading_system.intelligence import parse_limits
    observed, child, clock = controlled_child(monkeypatch)
    original_communicate = child.communicate
    def communicate(*args, **kwargs):
        if child.calls and child.returncode is None and kwargs.get("timeout") is not None:
            raise subprocess.TimeoutExpired("test-fixture-parser", kwargs["timeout"])
        try:
            return original_communicate(*args, **kwargs)
        except subprocess.TimeoutExpired:
            clock.now = 10.5
            raise
    def observe(*_args):
        if failed_observer == "missing":
            return None
        raise IntelligenceError("PARSER_RESOURCE_UNAVAILABLE", details={"observer_reason": "PS_TIMEOUT"})
    monkeypatch.setattr(child, "communicate", communicate)
    monkeypatch.setattr(parse_limits, "_resident_bytes", observe)
    result = extract_response(observed, policy=ParsePolicy(timeout_seconds=1))
    assert result.metadata["extraction_error"] == "PARSER_RESOURCE_UNAVAILABLE"
    expected = "LIVE_PROCESS_NOT_OBSERVED" if failed_observer == "missing" else "PS_TIMEOUT"
    assert result._parser_runtime["observer_reason"] == expected
    assert result._parser_runtime["peak_resident_bytes"] is None
    assert result.text == "" and result.completeness == "partial" and child.poll() is not None


def test_actual_resident_memory_guard_kills_over_limit_child(monkeypatch):
    from trading_system.intelligence import parse_limits
    original = parse_limits.subprocess.Popen
    children = []
    def start(_args, **kwargs):
        if not any(str(value).endswith("parse_worker.py") for value in _args):
            return original(_args, **kwargs)
        child = original([sys.executable, "-I", "-c",
            # Allocation size alone is not an RSS measurement. Random,
            # nonzero pages make this a resident-memory guard exercise.
            "import os,time; content = bytearray(os.urandom(160 * 1024 * 1024)); time.sleep(3)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(parse_limits.subprocess, "Popen", start)
    result = extract_response(response(b"<article>Original</article>"),
        policy=ParsePolicy(memory_bytes=128 * 1024 * 1024))
    assert result.metadata["extraction_error"] == "PARSER_MEMORY_EXCEEDED", result._parser_runtime
    assert result._parser_runtime["peak_resident_bytes"] > 128 * 1024 * 1024
    assert result.text == "" and result.completeness == "partial"
    assert children and all(child.poll() is not None for child in children)


def test_execution_runtime_stays_outside_deterministic_source_normalization():
    from trading_system.intelligence.parse_limits import capture_parse_runtime
    observed = response(b"<title>Original</title><article><h1>Original</h1><p>Complete source body.</p></article>")
    with capture_parse_runtime() as diagnostics:
        first = extract_response(observed, policy=ParsePolicy(html_engine="scrapling"))
        second = extract_response(observed, policy=ParsePolicy(html_engine="scrapling"))
    assert first.completeness == second.completeness == "complete"
    project = lambda result: {key: value for key, value in asdict(result).items() if key != "raw_bytes"}
    assert canonical(project(first)) == canonical(project(second))
    assert "parser_runtime" not in first.metadata
    assert len(diagnostics["records"]) == 2 and diagnostics["omitted_records"] == 0
    assert all(row["worker_phase"] == "output_ready" for row in diagnostics["records"])
    assert diagnostics["records"][0] is first._parser_runtime
    extract_response(observed, policy=ParsePolicy(html_engine="scrapling"))
    assert len(diagnostics["records"]) == 2


def test_parser_phase_diagnostics_discard_untrusted_stderr(monkeypatch):
    from trading_system.intelligence import parse_limits
    original = parse_limits.subprocess.Popen
    def start(args, **kwargs):
        if not any(str(value).endswith("parse_worker.py") for value in args):
            return original(args, **kwargs)
        wrapper = ("import os,sys; sys.stderr.write('CANARY_UNTRUSTED_SOURCE_WARNING\\n"
            "tiger-parser-phase:CANARY_UNTRUSTED_SOURCE_WARNING\\n'); sys.stderr.flush(); "
            "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])")
        return original([sys.executable, "-I", "-c", wrapper, *args[1:]], **kwargs)
    monkeypatch.setattr(parse_limits.subprocess, "Popen", start)
    with parse_limits.capture_parse_runtime() as diagnostics:
        result = extract_response(response(b"<title>Original</title><article><h1>Original</h1><p>Public body.</p></article>"),
            policy=ParsePolicy(html_engine="scrapling"))
    assert result.completeness == "complete"
    assert result._parser_runtime["worker_phase"] == "output_ready"
    assert b"CANARY_UNTRUSTED_SOURCE_WARNING" not in canonical(diagnostics)
    assert b"CANARY_UNTRUSTED_SOURCE_WARNING" not in canonical(result.metadata)


def news_layout_fixture(*, ancestor="aside", body="TEST_FIXTURE Original company revenue increased to 100 units."):
    recommendations = "".join('<article class="index-item"><h2>TEST_FIXTURE Other news ' + str(index)
        + "</h2><p>Different document preview.</p></article>" for index in range(5))
    parent = {"aside": ('<aside class="widget article-news">', "</aside>"),
        "related": ('<div class="related-news">', "</div>"),
        "recommended": ('<section aria-label="Recommended News">', "</section>"),
        "unmarked": ("<section>", "</section>")}[ancestor]
    return ('<html><title>TEST_FIXTURE Current operating release</title><h1 class="article-title">'
        'TEST_FIXTURE Current operating release</h1><div class="main-content"><div class="article-body"><p>'
        + body + '</p><table><tr><td>Original unit</td><td>100</td></tr></table></div></div>'
        + parent[0] + '<div class="widget-content"><div class="index">' + recommendations
        + "</div></div>" + parent[1] + "</html>").encode()


@pytest.mark.parametrize("ancestor", ["aside", "related", "recommended", "unmarked"])
def test_default_selector_cannot_promote_related_cards_without_current_headline(ancestor):
    result = extract_response(response(news_layout_fixture(ancestor=ancestor)), policy=ParsePolicy(html_engine="scrapling"))
    assert result.completeness == "partial"
    assert result.metadata["extraction_error"] == "PARSER_SELECTOR_UNVERIFIED"
    assert result.metadata["selector_validation"]["validated"] is False


def test_explicit_body_selector_and_body_terms_allow_title_outside_body():
    result = extract_response(response(news_layout_fixture()), policy=ParsePolicy(html_engine="scrapling",
        selector=".article-body", expected_terms=("Original company revenue",)))
    assert result.completeness == "complete"
    assert "Original company revenue increased to 100 units." in result.text
    assert "Other news" not in result.text and "Different document preview" not in result.text
    assert result.title == "TEST_FIXTURE Current operating release"
    assert " | " in result.text and result.metadata["selector_validation"]["selected_roots"] == 1


@pytest.mark.parametrize("terms", [(), ("Current operating release",)])
def test_custom_body_without_header_requires_matching_terms_in_body(terms):
    result = extract_response(response(news_layout_fixture()), policy=ParsePolicy(html_engine="scrapling",
        selector=".article-body", expected_terms=terms))
    assert result.completeness == "partial"
    assert result.metadata["extraction_error"] == "PARSER_SELECTOR_UNVERIFIED"


@pytest.mark.parametrize("body", ["", '<div id="dynamic-app"></div>', "<p>Loading...</p>",
    "<p>Please enable JavaScript</p>"])
def test_empty_or_dynamic_article_with_only_headline_is_not_complete(body):
    raw = ('<title>TEST_FIXTURE Current release</title><article><h1>TEST_FIXTURE Current release</h1>'
        + body + "</article>").encode()
    result = extract_response(response(raw), policy=ParsePolicy(html_engine="scrapling"))
    assert result.completeness == "partial"
    assert result.metadata["extraction_error"] == "PARSER_SELECTOR_UNVERIFIED"


def test_selected_main_omits_nested_related_material_but_keeps_original_table():
    raw = (b"<main><h1>TEST_FIXTURE Current release</h1><p>Original company operating evidence.</p>"
        b"<table><tr><td>Revenue</td><td>100</td></tr></table><div class='related-news'>"
        b"<article><h2>Other company release</h2><p>UNRELATED_RECOMMENDATION</p></article></div></main>")
    result = extract_response(response(raw), policy=ParsePolicy(html_engine="scrapling"))
    assert result.completeness == "complete"
    assert "Original company operating evidence." in result.text and "100" in result.text
    assert "UNRELATED_RECOMMENDATION" not in result.text


def test_slow_worker_startup_receives_complete_large_input_across_observation_slices(monkeypatch):
    """A delayed reader must not strand an unfinished first pipe write."""
    from trading_system.intelligence import parse_limits
    original = parse_limits.subprocess.Popen
    children = []
    def start(args, **kwargs):
        if not any(str(value).endswith('parse_worker.py') for value in args):
            return original(args, **kwargs)
        wrapper = ('import os,sys,time; time.sleep(0.35); '
                   'os.execv(sys.executable, [sys.executable, *sys.argv[1:]])')
        child = original([sys.executable, '-I', '-c', wrapper, *args[1:]], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(parse_limits.subprocess, 'Popen', start)
    body = (b'<title>TEST_FIXTURE Original</title><article><h1>TEST_FIXTURE Original</h1><p>'
        + b'Complete original evidence. ' * 12000 + b'FINAL_ORIGINAL_SENTINEL</p></article>')
    started = time.monotonic()
    result = extract_response(response(body), policy=ParsePolicy(html_engine='scrapling', timeout_seconds=4))
    assert result.completeness == 'complete', result._parser_runtime
    assert 'FINAL_ORIGINAL_SENTINEL' in result.text and result.raw_bytes == body
    assert time.monotonic() - started < 4
    assert children and all(child.poll() is not None for child in children)


def test_actual_image_only_pdf_keeps_original_and_explicit_ocr_gap():
    """A valid PDF with one raster XObject has no invented text/OCR result."""
    pixels = bytes([255, 255, 255, 0, 0, 0, 0, 0, 0, 255, 255, 255])
    drawing = b'q 100 0 0 100 0 0 cm /ScanImage Do Q'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] '
        b'/Resources << /XObject << /ScanImage 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB '
        b'/BitsPerComponent 8 /Length 12 >>\nstream\n' + pixels + b'\nendstream',
        b'<< /Length ' + str(len(drawing)).encode() + b' >>\nstream\n' + drawing + b'\nendstream',
    ]
    raw = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
    xref = len(raw)
    raw.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        raw.extend(f'{offset:010d} 00000 n \n'.encode())
    raw.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n')
    body = bytes(raw)
    result = extract_response(response(body, 'application/pdf'), policy=ParsePolicy())
    assert result.raw_bytes == body and result.completeness == 'partial' and result.text == ''
    assert result.metadata['extraction_error'] == 'IMAGE_ONLY_PDF'
    assert len(result.metadata['page_locations']) == 1
