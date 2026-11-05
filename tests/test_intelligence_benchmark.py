"""Independent benchmark assertions; no product acquisition or model calls."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from trading_system.intelligence_benchmark import (
    BenchmarkError, CATEGORIES, compare_captures, digest, evaluate_capture,
    load_json, validate_dataset,
)

FIXTURES = Path(__file__).parent / "fixtures" / "intelligence_benchmark"


@pytest.fixture
def inputs():
    dataset = load_json(FIXTURES / "dataset.json")
    capture = load_json(FIXTURES / "capture-ideal.json")
    return dataset, capture


def _single(inputs):
    dataset, capture = copy.deepcopy(inputs)
    dataset["cases"] = dataset["cases"][:1]
    capture["cases"] = capture["cases"][:1]
    capture["dataset_sha256"] = digest(dataset)
    return dataset, capture


def test_five_strata_hundred_cases_are_explicitly_fixtures(inputs):
    dataset, capture = inputs
    assert len(dataset["cases"]) == 100
    assert {x: sum(c["category"] == x for c in dataset["cases"]) for x in CATEGORIES} == dict.fromkeys(CATEGORIES, 20)
    report = evaluate_capture(dataset, capture)
    assert report["quality_conclusion"] == "fixture_only"
    assert report["overall"]["primary_evidence_recall"]["value"] == 1
    assert report["overall"]["latency_ms"]["p50"] == 215.5
    assert report["overall"]["cost"]["currency"] == "FIXTURE"
    assert report["strata"]["split"]["blind"]["cases"] == 75


def test_degraded_fixture_exposes_independent_defects(inputs):
    dataset, _ = inputs
    report = evaluate_capture(dataset, load_json(FIXTURES / "capture-degraded.json"))
    totals = report["overall"]
    assert totals["reference_url_recall"]["value"] == .8
    assert totals["qualified_event_recall"]["value"] == .4
    assert totals["primary_evidence_recall"]["value"] == .2
    assert totals["future_publication_documents"] == 20
    assert totals["wrong_identity_reference_documents"] == 20
    assert totals["duplicate_origin_documents"] == 20
    assert totals["cost"]["amount"] is None
    assert totals["cost"]["cases_unknown"] == 100


def test_missing_failed_empty_cases_remain_in_denominator(inputs):
    dataset, capture = inputs
    capture["cases"] = capture["cases"][:1]
    report = evaluate_capture(dataset, capture)
    totals = report["overall"]
    assert totals["case_attempt_coverage"]["value"] == .01
    assert totals["primary_evidence_recall"]["denominator"] == 100
    assert totals["capture_status"]["not_run"] == 99
    assert totals["latency_ms"]["missing"] == 99


@pytest.mark.parametrize("change", [
    {"published_at": "2024-02-03T00:00:00Z"},
    {"published_at": "2024-02-01"},
    {"published_at": "2024-02-01T14:00:00"},
    {"first_seen_at": "2024-02-03T00:00:00Z"},
    {"first_seen_at": None},
    {"event_ids": ["wrong-event"]},
    {"subject_ids": ["wrong-subject"]},
    {"label_status": "unreviewed"},
])
def test_wrong_identity_undated_future_and_unreviewed_not_qualified(inputs, change):
    dataset, capture = _single(inputs)
    capture["cases"][0]["documents"][0].update(change)
    report = evaluate_capture(dataset, capture)
    assert report["overall"]["reference_url_recall"]["value"] == 1
    assert report["overall"]["qualified_event_recall"]["value"] == 0


@pytest.mark.parametrize("change", [
    {"content": "altered"}, {"content_sha256": "0" * 64},
    {"evidence_quote": "not in original"}, {"content": ""},
])
def test_primary_recall_requires_exact_original_body_hash_and_span(inputs, change):
    dataset, capture = _single(inputs)
    capture["cases"][0]["documents"][0].update(change)
    totals = evaluate_capture(dataset, capture)["overall"]
    assert totals["qualified_event_recall"]["value"] == 1
    assert totals["primary_evidence_recall"]["value"] == 0


def test_late_immutable_version_needs_reviewed_hash_bound_seed(inputs):
    dataset, capture = _single(inputs)
    doc = capture["cases"][0]["documents"][0]
    doc["first_seen_at"] = "2024-03-01T00:00:00Z"
    event = dataset["cases"][0]["expected"][0]
    event["immutable_versions"] = [{"url": doc["url"], "content_sha256": doc["content_sha256"],
                                    "available_at": "2024-02-01T14:00:00Z", "proof_ref": "fixture immutable record"}]
    capture["dataset_sha256"] = digest(dataset)
    assert evaluate_capture(dataset, capture)["overall"]["primary_evidence_recall"]["value"] == 1
    event["immutable_versions"][0]["content_sha256"] = "0" * 64
    capture["dataset_sha256"] = digest(dataset)
    assert evaluate_capture(dataset, capture)["overall"]["primary_evidence_recall"]["value"] == 0


def test_empty_expected_set_is_unverified_not_perfect_score(inputs):
    dataset, capture = _single(inputs)
    dataset["cases"][0]["expected"] = []
    capture["cases"][0]["documents"] = []
    capture["dataset_sha256"] = digest(dataset)
    result = evaluate_capture(dataset, capture)["overall"]["primary_evidence_recall"]
    assert result == {"numerator": 0, "denominator": 0, "value": None, "state": "unverified"}


def test_same_origin_reposts_do_not_inflate_event_recall(inputs):
    dataset, capture = _single(inputs)
    doc = capture["cases"][0]["documents"][0]
    capture["cases"][0]["documents"].extend([copy.deepcopy(doc), copy.deepcopy(doc)])
    totals = evaluate_capture(dataset, capture)["overall"]
    assert totals["primary_events_found"] == 1
    assert totals["duplicate_origin_documents"] == 2
    assert totals["duplicate_origin_rate"]["value"] == pytest.approx(2 / 3)


def test_repeated_quote_needs_exact_offsets_and_wrong_offsets_do_not_qualify(inputs):
    dataset, capture = _single(inputs)
    doc = capture["cases"][0]["documents"][0]
    quote = doc["evidence_quote"]
    doc["content"] += " " + quote
    doc["content_sha256"] = hashlib.sha256(doc["content"].encode()).hexdigest()
    assert evaluate_capture(dataset, capture)["overall"]["primary_events_found"] == 0
    start = doc["content"].index(quote)
    doc.update(evidence_start=start, evidence_end=start + len(quote))
    assert evaluate_capture(dataset, capture)["overall"]["primary_events_found"] == 1
    doc["evidence_start"] = start + 1
    assert evaluate_capture(dataset, capture)["overall"]["primary_events_found"] == 0


def test_source_restriction_and_forbidden_results_are_measured(inputs):
    dataset, capture = _single(inputs)
    dataset["cases"][0]["allowed_sources"] = ["reviewed-source"]
    capture["dataset_sha256"] = digest(dataset)
    totals = evaluate_capture(dataset, capture)["overall"]
    assert totals["disallowed_source_documents"] == 1
    assert totals["qualified_events_found"] == 0
    capture["cases"][0]["documents"][0]["url"] = dataset["cases"][0]["forbidden_urls"][0]
    assert evaluate_capture(dataset, capture)["overall"]["forbidden_documents"] == 1


def test_dataset_binding_rejects_changed_gold(inputs):
    dataset, capture = inputs
    dataset["cases"][0]["question"] += " changed"
    with pytest.raises(BenchmarkError, match="INVALID_CAPTURE"):
        evaluate_capture(dataset, capture)


def test_reviewed_gold_requires_independent_review_and_reviewed_labels(inputs):
    dataset, capture = _single(inputs)
    dataset["truth_kind"] = "reviewed_gold"
    with pytest.raises(BenchmarkError):
        validate_dataset(dataset)
    dataset["adjudication"] = {"reviewer": "independent-operator", "reviewed_at": "2024-02-03T00:00:00Z",
                               "method": "Reviewed primary seeds before capture unblinding", "independent_of_captures": True}
    capture["mode"] = "historical_reconstruction"
    capture["dataset_sha256"] = digest(dataset)
    assert evaluate_capture(dataset, capture)["overall"]["qualified_events_found"] == 0
    capture["cases"][0]["documents"][0]["label_status"] = "reviewed"
    assert evaluate_capture(dataset, capture)["overall"]["qualified_events_found"] == 1


def test_capture_modes_and_providers_cannot_be_silently_conflated(inputs):
    dataset, capture = inputs
    with pytest.raises(BenchmarkError, match="DUPLICATE_PROVIDER"):
        compare_captures(dataset, [capture, capture])
    capture["mode"] = "prospective"
    with pytest.raises(BenchmarkError, match="FIXTURE_CANNOT_PROVE_PRODUCTION"):
        evaluate_capture(dataset, capture)


@pytest.mark.parametrize("mutate", [
    lambda d, c: c["cases"].append(c["cases"][0]),
    lambda d, c: c["cases"][0].update(latency_ms=-1),
    lambda d, c: c["cases"][0].update(status="success"),
    lambda d, c: c["cases"][0]["documents"][0].update(primary=True),
    lambda d, c: c["cases"][0]["documents"][0].update(url="https://user:secret@example.com/path"),
    lambda d, c: c["cases"][0]["documents"][0].update(content_sha256="not-a-hash"),
])
def test_untrusted_capture_contract_is_closed(inputs, mutate):
    dataset, capture = inputs
    mutate(dataset, capture)
    with pytest.raises(BenchmarkError):
        evaluate_capture(dataset, capture)


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', 'not json'])
def test_json_input_rejects_ambiguous_or_nonfinite_values(tmp_path, payload):
    path = tmp_path / "input.json"
    path.write_text(payload)
    with pytest.raises(BenchmarkError):
        load_json(path)


def test_json_input_is_bounded(tmp_path):
    path = tmp_path / "large.json"
    path.write_text("[1,2,3]")
    with pytest.raises(BenchmarkError, match="INPUT_TOO_LARGE"):
        load_json(path, max_bytes=4)


def test_cli_replays_hundred_cases_without_network(tmp_path):
    destination = tmp_path / "report.json"
    proc = subprocess.run([sys.executable, "scripts/intelligence-benchmark.py", "--dataset", str(FIXTURES / "dataset.json"),
                           "--capture", str(FIXTURES / "capture-ideal.json"),
                           "--capture", str(FIXTURES / "capture-degraded.json"), "--out", str(destination)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout
    report = json.loads(destination.read_text())
    assert len(report["reports"]) == 2
    assert report["conclusion"] == "fixture_only"
    # Exclusive output creation protects an existing evidence artifact.
    again = subprocess.run(proc.args, capture_output=True, text=True, timeout=30)
    assert again.returncode == 3
    assert json.loads(again.stdout)["error"] == "OUTPUT_UNWRITABLE"
