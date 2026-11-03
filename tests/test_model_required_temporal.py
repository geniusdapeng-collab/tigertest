"""Frozen research dates and observed price dates; offline fixtures only."""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from trading_system.agent_evidence import (
    EvidenceInvalid, SNAPSHOT_SCHEMA, atomic_bytes, digest, file_ref, json_bytes,
    normalize, read_bytes, read_json, read_snapshot, verify_events,
)
from trading_system.llm import required
from trading_system.llm.required_proof import verify_required_model
from trading_system.redline.execution import RequiredModelFailed, REQUIRED_MODEL_STAGES, daily_stage_contract
from test_model_required_workflow import _admission, required_fixture


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _run(tmp_path, monkeypatch):
    execution, root, args, _, denied, _ = required_fixture(tmp_path, monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    assert denied == []
    return execution, root, asdict(output.result)


def _stage(execution, root, name):
    row = next(row for row in execution.summary() if row["stage"] == name)
    read = lambda role: read_snapshot(root, row[role + "_ref"], run_id=execution.run_id,
                                      stage=name, role=role)
    return read("input"), read("output")


def _payload(content):
    return json.loads(content.split("\n", 1)[1].split("\nJSON schema:\n", 1)[0])


def _save_snapshot(root, blob):
    content = json_bytes(blob)
    path = "evidence/" + digest(content) + ".json"
    atomic_bytes(root, path, content)
    return file_ref(root, path, SNAPSHOT_SCHEMA)


def _rewrite_events(root, events, replacements):
    previous = None
    for row in events:
        for key in ("input_ref", "output_ref", "previous_stage_ref"):
            for old, new in replacements:
                if row[key] == old:
                    row[key] = new
        row["previous_event_sha256"] = previous
        row["event_sha256"] = digest(json_bytes({key: value for key, value in row.items()
                                                if key != "event_sha256"}))
        previous = row["event_sha256"]
    atomic_bytes(root, "events.jsonl", b"".join(json_bytes(row) for row in events))


def _tamper_stage_and_request(root, stage, field, *, remove=False):
    """Make stage/request agree and rehash every link, leaving trusted inputs intact."""
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == stage and row["kind"] == "end")
    input_blob = read_json(root, end["input_ref"]["path"])
    output_blob = read_json(root, end["output_ref"]["path"])
    bound = input_blob["value"]
    if remove:
        bound.pop(field)
    elif field == "assessment":
        bound[field]["run_date"] = "1999-01-01"
    else:
        bound[field]["benchmark_last_bar"] = "1999-01-01"
    for call in output_blob["value"]["io_calls"]:
        request_blob = read_json(root, call["input_ref"]["path"])
        request = request_blob["value"]
        content = request["request"]["messages"][1]["content"]
        payload = _payload(content)
        if remove:
            payload.pop(field)
        else:
            payload[field] = copy.deepcopy(bound[field])
        prefix = content.split("\n", 1)[0]
        suffix = content.split("\nJSON schema:\n", 1)[1]
        request["request"]["messages"][1]["content"] = prefix + "\n" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ) + "\nJSON schema:\n" + suffix
        request["request_sha256"] = _hash(request["request"])
        call["input_ref"] = _save_snapshot(root, request_blob)
        proof_blob = read_json(root, call["output_ref"]["path"])
        proof_blob["value"]["request_sha256"] = request["request_sha256"]
        call["output_ref"] = _save_snapshot(root, proof_blob)
    replacements = [(end["input_ref"], _save_snapshot(root, input_blob)),
                    (end["output_ref"], _save_snapshot(root, output_blob))]
    _rewrite_events(root, events, replacements)


def test_every_actual_stage_request_has_frozen_assessment_and_final_observed_asof(tmp_path, monkeypatch):
    execution, root, result = _run(tmp_path, monkeypatch)
    expected = _admission(execution)["clock"]
    for stage in REQUIRED_MODEL_STAGES:
        inputs, output = _stage(execution, root, stage)
        assert inputs["assessment"] == expected
        for call in output["io_calls"]:
            request = read_snapshot(root, call["input_ref"], run_id=execution.run_id,
                                    stage=stage, role="input")
            payload = _payload(request["request"]["messages"][1]["content"])
            assert payload["assessment"] == expected
            if stage == "decision.debate":
                _, prepared = _stage(execution, root, "data.prepare")
                symbols = [row["ticker"] for row in payload["approved_picks"] + payload["watch_candidates"]]
                actual = required.market_asof(prepared["value"]["market_data"], symbols, expected)
                assert payload["market_asof"] == inputs["market_asof"] == actual
                actual_market = required.market_snapshots(prepared["value"]["market_data"], symbols, expected)
                assert payload["market_evidence"] == inputs["market_evidence"] == actual_market
    assert verify_required_model(root, result, _admission(execution)) == result["raw"]["model"]


@pytest.mark.parametrize("published", [None, "1999-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_research_date_is_never_inferred_from_latest_or_unknown_document(published):
    calls = []
    execution = SimpleNamespace(run_date=date(2026, 10, 26),
                                policy=SimpleNamespace(market_timezone="America/New_York"))
    def complete_json(**kwargs):
        calls.append(kwargs)
        return {"ok": True}
    client = SimpleNamespace(execution=execution, complete_json=complete_json)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    required._call(client, "tech.risk", {"documents": [{"published_at": published}]}, schema)
    assert calls[0]["public_payload"]["assessment"] == {
        "run_date": "2026-10-26", "market_timezone": "America/New_York"}
    assert "不能把资料中的日期当作当前日期" in calls[0]["system"]
    assert "published_at为空代表发布时间未知" in calls[0]["system"]
    assert "转载即使位于不同网站也只算一个原始来源" in calls[0]["system"]
    assert "AI生成的评论或第三方断言须明确标注性质" in calls[0]["system"]
    assert "不代表实时价格" in calls[0]["system"]


@pytest.mark.parametrize("run_date,timezone", [
    (None, "America/New_York"), ("2026-10-26", "America/New_York"),
    (datetime(2026, 10, 26), "America/New_York"), (date(2026, 10, 26), "UTC"),
])
def test_assessment_requires_the_frozen_execution_date_type_and_market_timezone(run_date, timezone):
    with pytest.raises(RequiredModelFailed):
        required.assessment_context(SimpleNamespace(run_date=run_date,
            policy=SimpleNamespace(market_timezone=timezone)))


def _market_data():
    frame = lambda day: pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime([day]))
    return {"spy": frame("2026-10-23"),
            "freshness": {"benchmark_last_bar": "2026-10-23", "benchmark_lag_days": 3},
            "stock_ohlcv": {"AAA": frame("2026-10-22"), "BBB": frame("2026-10-21")},
            "sector_etfs": {"XLK": frame("2026-10-22"), "XLF": frame("2026-10-23")}}


@pytest.mark.parametrize("normalized", [False, True])
def test_market_asof_retains_different_actual_stock_and_sector_bar_dates(normalized):
    data = _market_data()
    if normalized:
        data = normalize(data)
    assessment = {"run_date": "2026-10-26", "market_timezone": "America/New_York"}
    assert required.market_asof(data, ["AAA", "BBB"], assessment) == {
        "benchmark_last_bar": "2026-10-23", "benchmark_lag_days": 3,
        "selected_stock_last_bar": {"AAA": "2026-10-22", "BBB": "2026-10-21"},
        "sector_last_bar": {"XLK": "2026-10-22", "XLF": "2026-10-23"}}


@pytest.mark.parametrize("corrupt", [
    "benchmark_date", "lag", "bool_lag", "missing_stock", "empty_stock",
    "nat_stock", "malformed_normalized_stock",
])
def test_market_asof_rejects_missing_or_inconsistent_observations(corrupt):
    data = _market_data()
    if corrupt == "benchmark_date":
        data["freshness"]["benchmark_last_bar"] = "2026-10-22"
    elif corrupt == "lag":
        data["freshness"]["benchmark_lag_days"] = 1
    elif corrupt == "bool_lag":
        data["freshness"]["benchmark_lag_days"] = False
    elif corrupt == "missing_stock":
        data["stock_ohlcv"].pop("AAA")
    elif corrupt == "empty_stock":
        data["stock_ohlcv"]["AAA"] = data["stock_ohlcv"]["AAA"].iloc[:0]
    elif corrupt == "nat_stock":
        data["stock_ohlcv"]["AAA"].index = pd.DatetimeIndex([pd.NaT])
    else:
        data = normalize(data)
        data["stock_ohlcv"]["AAA"]["index"][0]["value"] = "not-a-timestamp"
    with pytest.raises(RequiredModelFailed):
        required.market_asof(data, ["AAA"], {"run_date": "2026-10-26", "market_timezone": "America/New_York"})


@pytest.mark.parametrize("clock", [
    {}, {"run_date": "2026-10-20"}, {"market_timezone": "America/New_York"},
    {"run_date": "20261020", "market_timezone": "America/New_York"},
    {"run_date": "2026-02-30", "market_timezone": "America/New_York"},
    {"run_date": "2026-10-20T00:00:00", "market_timezone": "America/New_York"},
    {"run_date": None, "market_timezone": "America/New_York"},
    {"run_date": "2026-10-20", "market_timezone": "UTC"},
])
def test_proof_rejects_missing_or_invalid_admission_temporal_authority(tmp_path, clock):
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(tmp_path, {}, {"clock": clock})
    assert error.value.code == "MODEL_REQUIRED_FAILED"


@pytest.mark.parametrize("stage", REQUIRED_MODEL_STAGES)
@pytest.mark.parametrize("remove", [False, True])
def test_rehashed_stage_and_request_cannot_override_or_omit_frozen_assessment(
        tmp_path, monkeypatch, stage, remove):
    execution, root, result = _run(tmp_path, monkeypatch)
    _tamper_stage_and_request(root, stage, "assessment", remove=remove)
    verify_events(root, execution.run_id, daily_stage_contract(True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(root, result, _admission(execution))
    assert error.value.code == "MODEL_REQUIRED_FAILED"


@pytest.mark.parametrize("remove", [False, True])
def test_rehashed_final_stage_and_request_cannot_replace_observed_market_asof(tmp_path, monkeypatch, remove):
    execution, root, result = _run(tmp_path, monkeypatch)
    _tamper_stage_and_request(root, "decision.debate", "market_asof", remove=remove)
    verify_events(root, execution.run_id, daily_stage_contract(True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(root, result, _admission(execution))
    assert error.value.code == "MODEL_REQUIRED_FAILED"


def test_proof_converts_inconsistent_prepared_market_timestamp_to_evidence_failure(tmp_path, monkeypatch):
    execution, root, result = _run(tmp_path, monkeypatch)
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == "data.prepare" and row["kind"] == "end")
    blob = read_json(root, end["output_ref"]["path"])
    blob["value"]["value"]["market_data"]["freshness"]["benchmark_last_bar"] = "1999-01-01"
    _rewrite_events(root, events, [(end["output_ref"], _save_snapshot(root, blob))])
    verify_events(root, execution.run_id, daily_stage_contract(True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(root, result, _admission(execution))
    assert error.value.code == "MODEL_REQUIRED_FAILED"
