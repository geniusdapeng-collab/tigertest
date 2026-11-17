"""MC-010 progress projection is atomic and never a receipt."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_system.agent_evidence import atomic_json
from trading_system.agent_runner import _progress_projection


def progress_value(**changes):
    value = {
        "schema": "tiger.run-progress/v1",
        "stage": "data.prepare",
        "provider": "tencent",
        "completed": 12,
        "failed": 1,
        "timed_out": 2,
        "attempted": 15,
        "total": 20,
        "current_chunk": 3,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "deadline": datetime.now(timezone.utc).isoformat(),
        "progress_is_not_receipt": True,
    }
    value.update(changes)
    return value


def test_progress_projection_is_readable_and_explicitly_not_a_receipt(tmp_path):
    run = tmp_path / "run"
    work = run / "work"
    work.mkdir(parents=True)
    value = progress_value()
    atomic_json(work, "progress.json", value)
    assert _progress_projection(run) == value
    assert value["completed"] + value["failed"] + value["timed_out"] <= value["total"]


def test_missing_or_tampered_progress_is_unavailable_without_blocking_status(tmp_path):
    run = tmp_path / "run"
    assert _progress_projection(run) is None
    work = run / "work"
    work.mkdir(parents=True)
    atomic_json(work, "progress.json", progress_value(progress_is_not_receipt=False))
    value = _progress_projection(run)
    assert value["unavailable"] is True
    assert value["progress_is_not_receipt"] is True
