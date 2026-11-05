"""Frozen old-corpus replay never fabricates fresh semantic/original evidence."""
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import subprocess
import sys

import pytest

import main
from trading_system.agent_evidence import EvidenceInvalid, atomic_json
from trading_system.agent_runtime import SOURCE_ROOT
from trading_system.intelligence_replay import replay_native_run
from trading_system.redline.execution import daily_stage_contract
from trading_system.search.models import StructuredDocument
from test_model_required_workflow import required_fixture


def frozen_native(tmp_path, monkeypatch):
    execution, root, args, _, denied, _ = required_fixture(tmp_path, monkeypatch)
    main._daily(args, "yahoo", execution=execution)
    atomic_json(root, "admission.json", {"clock": {"admitted_at_utc": "2026-10-20T22:00:00+00:00"}})
    atomic_json(root, "policy.json", {"stages": daily_stage_contract(True)})
    assert denied == []
    return root


def test_frozen_corpus_replay_explains_each_document_without_old_semantic_upgrade(tmp_path, monkeypatch):
    root = frozen_native(tmp_path, monkeypatch)
    report = replay_native_run(root)
    assert report["document_count"] == 14 and len(report["documents"]) == 14
    assert report["typed_status_counts"] == {"semantic_annotation_missing": 14}
    assert report["network_requests"] == 0 and report["supplied_annotations"] == 0
    assert report["model_provenance_verified"] is False
    assert report["production_acceptance"] == "unverified"
    assert all(not row["scorable"] and not row["action_authorized"] for row in report["documents"])
    assert all("ORIGINAL_WEBSITE_BODY_NOT_CAPTURED" in row["blockers"] for row in report["documents"])


@pytest.mark.parametrize("bad", ["id", "hash", "eligibility", "duplicate"])
def test_unbound_or_permission_bearing_annotations_are_rejected(tmp_path, monkeypatch, bad):
    root = frozen_native(tmp_path, monkeypatch)
    original = replay_native_run(root)["documents"][0]
    annotation = {"tickers": [], "sentiment": "neutral", "sentiment_score": 0, "events": [],
        "summary_zh": "离线标注夹具", "relevance": 0, "subjects": [], "evidence_spans": [], "event_records": [], "claims": []}
    row = {"id": original["document_id"], "source_sha256": original["source_sha256"], "annotation": annotation}
    if bad == "id":
        row["id"] = "different-run-document"
    elif bad == "hash":
        row["source_sha256"] = "f" * 64
    elif bad == "eligibility":
        row["annotation"]["eligibility"] = {"allowed_uses": ["action"]}
    rows = [row, row] if bad == "duplicate" else [row]
    with pytest.raises(EvidenceInvalid):
        replay_native_run(root, {"schema": "tiger.typed-replay-input/v1", "documents": rows})


def test_external_annotation_remains_declared_not_verified_model_provenance(tmp_path, monkeypatch):
    root = frozen_native(tmp_path, monkeypatch)
    original = replay_native_run(root)["documents"][0]
    annotation = {"tickers": [], "sentiment": "neutral", "sentiment_score": 0, "events": [],
        "summary_zh": "离线标注夹具", "relevance": 0, "subjects": [], "evidence_spans": [], "event_records": [], "claims": []}
    report = replay_native_run(root, {"schema": "tiger.typed-replay-input/v1", "documents": [
        {"id": original["document_id"], "source_sha256": original["source_sha256"], "annotation": annotation}]})
    assert report["supplied_annotations"] == 1 and report["model_provenance_verified"] is False
    assert sum(row["scorable"] for row in report["documents"]) == 0


def test_replay_cli_missing_evidence_fails_without_partial_report(tmp_path):
    out = tmp_path / "not-written.json"
    proc = subprocess.run([sys.executable, str(SOURCE_ROOT / "scripts/intelligence-replay.py"),
        "--run-root", str(tmp_path / "missing"), "--out", str(out)], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 2 and '"network_requests": 0' in proc.stdout and not out.exists()
