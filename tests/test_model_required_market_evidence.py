"""Real frame bindings and citation rejection; all provider/model IO is offline."""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from trading_system import pipeline
from trading_system.agent_evidence import (EvidenceInvalid, normalize, read_bytes,
    read_json, read_snapshot, verify_events)
from trading_system.data_models import StockCandidate, TradePick
from trading_system.llm import required
from trading_system.llm import market_evidence
from trading_system.llm.market_evidence import market_snapshots
from trading_system.llm.required_proof import verify_required_model, _same_watch_facts
from trading_system.llm.required_schemas import decision_schema
from trading_system.redline.execution import RequiredModelFailed, daily_stage_contract
from trading_system.search.models import CleanDocument, RawDocument
from test_model_required_temporal import _payload, _save_snapshot, _rewrite_events
from test_model_required_workflow import _admission, required_fixture


ASSESSMENT = {"run_date": "2026-10-25", "market_timezone": "America/New_York"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _frame(day="2026-10-23", close=105.25):
    return pd.DataFrame({"Open": [99.0, 104.0], "Close": [100.0, close],
                         "Volume": [1000000, 2000000]},
                        index=pd.to_datetime(["2026-10-21", day]))


def _market():
    return {"spy": _frame(),
            "freshness": {"benchmark_last_bar": "2026-10-23", "benchmark_lag_days": 0},
            "stock_ohlcv": {"AAA": _frame(), "BBB": _frame("2026-10-22", 90.5)},
            "sector_etfs": {"XLK": _frame()}}


@pytest.mark.parametrize("frozen", [False, True])
def test_snapshot_binds_exact_last_close_and_full_typed_frame_without_newline(frozen):
    market = _market()
    canonical_frame = normalize(market["stock_ohlcv"]["AAA"])
    expected = _hash(canonical_frame)
    if frozen:
        market = normalize(market)
    assert market_snapshots(market, ["AAA"], ASSESSMENT) == [{
        "id": "md_AAA_20261023_" + expected[:24], "symbol": "AAA",
        "as_of": "2026-10-23", "close": 105.25, "series_sha256": expected}]
    assert expected != hashlib.sha256((_canonical(canonical_frame) + "\n").encode()).hexdigest()


@pytest.mark.parametrize("field", ["old_value", "old_index", "column_order", "dtype", "index_name"])
def test_snapshot_hash_includes_entire_history_and_metadata(field):
    market = _market()
    before = market_snapshots(market, ["AAA"], ASSESSMENT)[0]
    frame = market["stock_ohlcv"]["AAA"]
    if field == "old_value":
        frame.iloc[0, 0] += 1
    elif field == "old_index":
        frame.index = pd.to_datetime(["2026-10-20", "2026-10-23"])
    elif field == "column_order":
        market["stock_ohlcv"]["AAA"] = frame[["Volume", "Close", "Open"]]
    elif field == "dtype":
        market["stock_ohlcv"]["AAA"] = frame.astype({"Volume": "float64"})
    else:
        frame.index.name = "ObservedDate"
    after = market_snapshots(market, ["AAA"], ASSESSMENT)[0]
    assert after["close"] == before["close"] and after["as_of"] == before["as_of"]
    assert after["series_sha256"] != before["series_sha256"] and after["id"] != before["id"]


def test_empty_selection_requires_no_frame_and_stock_date_is_not_guessed_from_benchmark():
    assert market_snapshots({}, [], ASSESSMENT) == []
    market = _market()
    market["stock_ohlcv"]["AAA"] = _frame("2026-10-24")
    rows = market_snapshots(market, ["BBB", "AAA"], ASSESSMENT)
    assert [(row["symbol"], row["as_of"]) for row in rows] == [
        ("BBB", "2026-10-22"), ("AAA", "2026-10-24")]


@pytest.mark.parametrize("assessment", [
    {}, {"run_date": "2026-10-25"}, {**ASSESSMENT, "extra": 1},
    {**ASSESSMENT, "run_date": "20261025"}, {**ASSESSMENT, "run_date": "2026-02-30"},
    {**ASSESSMENT, "run_date": date(2026, 10, 25)}, {**ASSESSMENT, "market_timezone": "UTC"},
])
def test_snapshot_rejects_invalid_or_unfrozen_assessment(assessment):
    with pytest.raises(RequiredModelFailed):
        market_snapshots(_market(), ["AAA"], assessment)


@pytest.mark.parametrize("symbols", [["AAA", "AAA"], ["aaa"], ["../AAA"], [True], "AAA", ["MISSING"]])
def test_snapshot_rejects_invalid_duplicate_or_missing_symbols(symbols):
    with pytest.raises(RequiredModelFailed):
        market_snapshots(_market(), symbols, ASSESSMENT)


@pytest.mark.parametrize("close", [True, False, 0, -0.1, float("nan"), float("inf"), float("-inf"), None, "105.25"])
def test_snapshot_rejects_non_numeric_nonpositive_or_missing_last_close(close):
    market = normalize(_market())
    frame = market["stock_ohlcv"]["AAA"]
    frame["values"][-1][frame["columns"].index("Close")] = close
    with pytest.raises(RequiredModelFailed):
        market_snapshots(market, ["AAA"], ASSESSMENT)


@pytest.mark.parametrize("corrupt", [
    "missing_frame", "empty", "missing_close", "duplicate_close", "duplicate_other",
    "row_size", "row_count", "dtype_count", "unknown_key", "nat", "bad_date",
    "impossible_date", "numeric_index", "future", "wrong_date_type", "nonstring_column",
])
def test_snapshot_rejects_malformed_frames_dates_and_future_observations(corrupt):
    market = normalize(_market())
    frame = market["stock_ohlcv"]["AAA"]
    if corrupt == "missing_frame":
        market["stock_ohlcv"]["AAA"] = None
    elif corrupt == "empty":
        frame["index"], frame["values"] = [], []
    elif corrupt == "missing_close":
        frame["columns"][1] = "AdjustedClose"
    elif corrupt == "duplicate_close":
        frame["columns"][0] = "Close"
    elif corrupt == "duplicate_other":
        frame["columns"][2] = "Open"
    elif corrupt == "row_size":
        frame["values"][-1].pop()
    elif corrupt == "row_count":
        frame["values"].pop()
    elif corrupt == "dtype_count":
        frame["dtypes"].pop()
    elif corrupt == "unknown_key":
        frame["extra"] = "forged"
    elif corrupt == "nat":
        frame["index"][-1] = {"__type__": "missing", "reason": "pandas_na"}
    elif corrupt == "bad_date":
        frame["index"][-1]["value"] = "not-a-date"
    elif corrupt == "impossible_date":
        frame["index"][-1]["value"] = "2026-02-30T00:00:00"
    elif corrupt == "numeric_index":
        frame["index"][-1] = 20261023
    elif corrupt == "future":
        frame["index"][-1]["value"] = "2026-10-26T00:00:00"
    elif corrupt == "nonstring_column":
        frame["columns"][0] = 7
    else:
        frame["index"][-1] = {"__type__": "series", "value": "2026-10-23"}
    with pytest.raises(RequiredModelFailed):
        market_snapshots(market, ["AAA"], ASSESSMENT)


@pytest.mark.parametrize("frozen", [False, True])
def test_snapshot_generation_does_not_change_any_frame_or_input(frozen):
    market = normalize(_market()) if frozen else _market()
    before = normalize(market)
    market_snapshots(market, ["AAA", "BBB"], ASSESSMENT)
    assert normalize(market) == before


def test_zero_series_hash_is_never_a_market_identity(monkeypatch):
    monkeypatch.setattr(market_evidence.hashlib, "sha256",
                        lambda raw: SimpleNamespace(hexdigest=lambda: "0" * 64))
    with pytest.raises(RequiredModelFailed):
        market_snapshots(_market(), ["AAA"], ASSESSMENT)


def _decision_context():
    pick = TradePick("AAA", 8.0, 6.0, "A", 104.0, 99.0, 100, .104, 500.0, time_stop_days=5)
    context = {"market_data": _market(), "trade_date": "2026-10-25", "picks": [pick],
        "watchlist": [StockCandidate("AAA"), StockCandidate("BBB")], "action": "BUY",
        "pick_rationale": {"AAA": {"entry": 104.0, "stop": 99.0}},
        "mrs": None, "sectors": [], "rejections": {}}
    doc = CleanDocument(RawDocument("ds_fixture_article", "deepseek_search", "Fixture company disclosure",
        "https://example.test/company", "Fixture disclosed company information, never a live source.", "2026-10-23"))
    return context, [doc], pick


def _decision_client(action="watch", mode="own", captured=None):
    execution = SimpleNamespace(run_date=date(2026, 10, 25),
                                policy=SimpleNamespace(market_timezone="America/New_York"))
    def complete_json(**kwargs):
        payload = kwargs["public_payload"]
        if captured is not None:
            captured.append(payload)
        market = {row["symbol"]: row["id"] for row in payload["market_evidence"]}
        decisions = []
        for symbol in market:
            own = market[symbol]
            other = next(item for candidate, item in market.items() if candidate != symbol)
            refs = {"own": [own], "own_news": [own, "ds_fixture_article"], "none": [],
                    "news_only": ["ds_fixture_article"], "foreign": [other],
                    "multiple": [own, other], "duplicate": [own, own],
                    "forged": ["md_" + symbol + "_20261023_" + "a" * 24]}[mode]
            row_action = action if symbol == "AAA" else "watch"
            if symbol != "AAA":
                refs = [own]
            decisions.append({"symbol": symbol, "action": row_action, "action_summary": "离线夹具观察",
                "why_this": "观察已提供的价格结构", "why_now": "缺少相关公司新闻，仅可观察，时效与催化不确定",
                "entry_condition": "须另行核实公司事实并重新通过全部风控", "invalidation": "价格结构或证据失效即取消",
                "risk_note": "没有公司新闻确认，行情收盘参考不构成买入证据", "source_ids": refs})
        return {"action": "BUY" if action == "buy" else "WAIT",
                "action_summary": "离线夹具不构成实际交易研究", "decisions": decisions}
    return SimpleNamespace(execution=execution, complete_json=complete_json,
                           plan_chat=lambda **kwargs: None)


def test_market_only_watch_has_no_invented_news_and_no_allocation():
    context, docs, _ = _decision_context()
    payloads = []
    output = required.decision(_decision_client(captured=payloads), context, docs, 100000.0)
    assert output["model_output"]["action"] == "WAIT" and context["picks"] == []
    for row in output["execution_decisions"]:
        assert row["sources"] == [] and row["target_weight"] == 0
        assert len(row["market_evidence"]) == 1 and row["market_evidence"][0]["symbol"] == row["symbol"]
        assert all(row[key] is None for key in ("entry_price", "stop_price", "shares", "risk_usd", "time_stop_days"))
    assert payloads[0]["market_evidence"] == market_snapshots(_market(), ["AAA", "BBB"], ASSESSMENT)


def test_buy_requires_article_plus_own_snapshot_and_preserves_all_approved_numbers():
    context, docs, pick = _decision_context()
    output = required.decision(_decision_client("buy", "own_news"), context, docs, 100000.0)
    row = output["execution_decisions"][0]
    assert row["sources"][0]["id"] == docs[0].raw.doc_id
    assert row["market_evidence"] == market_snapshots(context["market_data"], ["AAA"], ASSESSMENT)
    assert (row["entry_price"], row["stop_price"], row["shares"], row["risk_usd"],
            row["target_weight"], row["time_stop_days"]) == (104.0, 99.0, 100, 500.0, .104, 5)
    assert context["picks"] == [pick] and asdict(pick)["entry_price"] != row["market_evidence"][0]["close"]


@pytest.mark.parametrize("action,mode", [
    ("watch", "none"), ("watch", "news_only"), ("watch", "foreign"),
    ("watch", "multiple"), ("watch", "duplicate"), ("watch", "forged"),
    ("buy", "own"), ("buy", "multiple"),
])
def test_missing_foreign_forged_market_and_buy_without_article_fail_closed(action, mode):
    context, docs, pick = _decision_context()
    with pytest.raises(RequiredModelFailed):
        required.decision(_decision_client(action, mode), context, docs, 100000.0)
    assert context["picks"] == [pick]


def _watch_run(tmp_path, monkeypatch, *, avoid=False):
    def answer(stage, output):
        if avoid and stage == "decision.debate":
            output["action"] = "AVOID"
        return output
    execution, root, args, _, denied, _ = required_fixture(tmp_path, monkeypatch, corrupt_answer=answer)
    original = pipeline.UniverseScannerAgent.execute
    def one_fixture_candidate(scanner, context):
        candidates = original(scanner, context)
        candidate = next((item for item in candidates if item.ticker == "AAA"), None)
        if candidate is None:
            frame = context["market_data"]["stock_ohlcv"]["AAA"]
            candidate = StockCandidate("AAA", price=float(frame["Close"].iloc[-1]),
                                       adv_usd=200000000.0, atr_pct=.02)
        context["watchlist"] = [candidate]
        context["scan_stats"]["selected"] = 1
        return context["watchlist"]
    monkeypatch.setattr(pipeline.UniverseScannerAgent, "execute", one_fixture_candidate)
    if avoid:
        original_mrs = pipeline.MRSAgent.execute
        def blocked_market(agent, context):
            mrs = original_mrs(agent, context)
            mrs.allow_new_positions = False
            return mrs
        monkeypatch.setattr(pipeline.MRSAgent, "execute", blocked_market)
    output = main._daily(args, "yahoo", execution=execution)
    assert denied == []
    return execution, root, asdict(output.result)


def test_all39_stages_accept_grounded_market_only_watch_and_independent_reconstruction(tmp_path, monkeypatch):
    execution, root, result = _watch_run(tmp_path, monkeypatch)
    execution.assert_complete()
    assert verify_events(root, execution.run_id, daily_stage_contract(True))["stage_count"] == 39
    rows = result["raw"]["execution_decisions"]
    assert len(rows) == 1 and rows[0]["symbol"] == "AAA" and rows[0]["action"] == "watch"
    assert rows[0]["sources"] == [] and len(rows[0]["market_evidence"]) == 1
    assert rows[0]["target_weight"] == 0 and result["picks"] == []
    assert verify_required_model(root, result, _admission(execution)) == result["raw"]["model"]


def test_all39_stages_accept_numeric_avoid_with_absent_risk_rationale(tmp_path, monkeypatch):
    execution, root, result = _watch_run(tmp_path, monkeypatch, avoid=True)
    execution.assert_complete()
    assert result["action"] == "AVOID" and result["picks"] == []
    assert verify_events(root, execution.run_id, daily_stage_contract(True))["stage_count"] == 39
    assert verify_required_model(root, result, _admission(execution)) == result["raw"]["model"]


def test_watch_binding_allows_only_two_explicit_mutable_values_and_preserves_all_field_keys():
    observed = normalize([StockCandidate("AAA", price=105.25, tos=0, sector_etf="")])
    actual = copy.deepcopy(observed)
    actual[0]["fields"].update(tos=6.0, sector_etf="XLK")
    assert _same_watch_facts(actual, observed)
    actual[0]["fields"].pop("tos")
    assert not _same_watch_facts(actual, observed)
    actual = copy.deepcopy(observed)
    actual[0]["fields"]["invented"] = 1
    assert not _same_watch_facts(actual, observed)
    actual = copy.deepcopy(observed)
    actual[0]["fields"]["s_structure"] = False
    assert not _same_watch_facts(actual, observed)


def _forge_final_context(root, field):
    """Keep final stage/request hashes self-consistent while earlier facts stay fixed."""
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == "decision.debate" and row["kind"] == "end")
    input_blob = read_json(root, end["input_ref"]["path"])
    output_blob = read_json(root, end["output_ref"]["path"])
    call = output_blob["value"]["io_calls"][0]
    request_blob = read_json(root, call["input_ref"]["path"])
    proof_blob = read_json(root, call["output_ref"]["path"])
    request = request_blob["value"]
    content = request["request"]["messages"][1]["content"]
    payload = _payload(content)
    bound = input_blob["value"]
    if field.startswith("candidate_"):
        current, supplied = bound["watchlist"][0]["fields"], payload["watch_candidates"][0]
        if field == "candidate_price":
            current["price"] += 100
            supplied["price"] = current["price"]
        elif field == "candidate_missing_key":
            current.pop("price"); supplied.pop("price")
        else:
            current["invented"] = supplied["invented"] = 1
    elif field == "mrs":
        bound["mrs"]["fields"]["mrs_star"] += 1
        payload["mrs"] = copy.deepcopy(bound["mrs"])
    elif field == "sectors":
        bound["sectors"][0]["fields"]["shs"] += 1
        payload["sectors"] = copy.deepcopy(bound["sectors"])
    elif field == "action":
        bound["action"] = payload["numeric_action"] = "BUY"
    elif field == "rationale":
        bound["rationale"] = {"AAA": {"invented": "unbound risk reasoning"}}
        payload["risk_rationale"] = copy.deepcopy(bound["rationale"])
    else:
        bound["rejections"] = payload["rejections"] = {"invented": True}
    suffix = content.split("\nJSON schema:\n", 1)[1]
    request["request"]["messages"][1]["content"] = content.split("\n", 1)[0] + "\n" + _canonical(payload) + "\nJSON schema:\n" + suffix
    request["request_sha256"] = _hash(request["request"])
    proof_blob["value"]["request_sha256"] = request["request_sha256"]
    call["input_ref"], call["output_ref"] = _save_snapshot(root, request_blob), _save_snapshot(root, proof_blob)
    _rewrite_events(root, events, [(end["input_ref"], _save_snapshot(root, input_blob)),
                                   (end["output_ref"], _save_snapshot(root, output_blob))])


@pytest.mark.parametrize("field", ["candidate_price", "candidate_missing_key", "candidate_extra_key",
    "mrs", "sectors", "action", "rationale", "rejections"])
def test_rehashed_final_context_cannot_replace_prior_candidate_market_and_risk_facts(tmp_path, monkeypatch, field):
    execution, root, result = _watch_run(tmp_path, monkeypatch)
    _forge_final_context(root, field)
    verify_events(root, execution.run_id, daily_stage_contract(True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(root, result, _admission(execution))
    assert error.value.code == "MODEL_REQUIRED_FAILED"


def _forge_bound_market(root, result, field):
    """Rehash self-consistent final input/call/result; leave actual data.prepare intact."""
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == "decision.debate" and row["kind"] == "end")
    input_blob = read_json(root, end["input_ref"]["path"])
    output_blob = read_json(root, end["output_ref"]["path"])
    old = input_blob["value"]["market_evidence"][0]
    forged = copy.deepcopy(old)
    if field == "selected_symbol":
        prepared = next(row for row in events if row["stage"] == "data.prepare" and row["kind"] == "end")
        market = read_snapshot(root, prepared["output_ref"], run_id=root.name,
                               stage="data.prepare", role="output")["value"]["market_data"]
        forged = market_snapshots(market, ["BBB"], {"run_date": result["trade_date"],
            "market_timezone": "America/New_York"})[0]
        input_blob["value"]["watchlist"][0]["fields"]["ticker"] = "BBB"
        input_blob["value"]["market_asof"]["selected_stock_last_bar"] = {"BBB": forged["as_of"]}
    elif field == "close":
        forged["close"] += 10
    elif field == "date":
        forged["as_of"] = "2026-10-19"
    else:
        forged["series_sha256"] = "f" * 64
    forged["id"] = "md_" + forged["symbol"] + "_" + forged["as_of"].replace("-", "") + "_" + forged["series_sha256"][:24]
    input_blob["value"]["market_evidence"] = [forged]
    call = output_blob["value"]["io_calls"][0]
    request_blob = read_json(root, call["input_ref"]["path"])
    proof_blob = read_json(root, call["output_ref"]["path"])
    request, proof = request_blob["value"], proof_blob["value"]
    content = request["request"]["messages"][1]["content"]
    payload = _payload(content)
    payload["market_evidence"] = [forged]
    if field == "selected_symbol":
        payload["watch_candidates"][0]["ticker"] = "BBB"
        payload["market_asof"] = copy.deepcopy(input_blob["value"]["market_asof"])
    symbols = [item["ticker"] for item in payload["approved_picks"] + payload["watch_candidates"]]
    schema = decision_schema(symbols, [item["id"] for item in payload["documents"]], [forged["id"]])
    request["output_schema"] = schema
    request["schema_sha256"] = _hash(schema)
    request["request"]["messages"][1]["content"] = content.split("\n", 1)[0] + "\n" + _canonical(payload) + "\nJSON schema:\n" + _canonical(schema) + "\nJSON field guidance:\n{}"
    request["request_sha256"] = _hash(request["request"])
    proof["schema_sha256"], proof["request_sha256"] = request["schema_sha256"], request["request_sha256"]
    for row in proof["parsed"]["decisions"]:
        row["source_ids"] = [forged["id"] if item == old["id"] else item for item in row["source_ids"]]
        if field == "selected_symbol":
            row["symbol"] = "BBB"
    proof["response"]["choices"][0]["message"]["content"] = _canonical(proof["parsed"])
    proof["response_sha256"] = _hash(proof["response"])
    call["input_ref"], call["output_ref"] = _save_snapshot(root, request_blob), _save_snapshot(root, proof_blob)
    value = output_blob["value"]["value"]
    value["model_output"] = copy.deepcopy(proof["parsed"])
    for row in value["execution_decisions"]:
        row["market_evidence"] = [copy.deepcopy(forged)]
        if field == "selected_symbol":
            row["symbol"] = "BBB"
    if field == "selected_symbol":
        value["triggered"] = ["BBB"]
        result["watchlist"][0]["ticker"] = "BBB"
    result["raw"]["execution_decisions"] = copy.deepcopy(value["execution_decisions"])
    _rewrite_events(root, events, [(end["input_ref"], _save_snapshot(root, input_blob)),
                                   (end["output_ref"], _save_snapshot(root, output_blob))])


@pytest.mark.parametrize("field", ["close", "date", "hash", "selected_symbol"])
def test_rehashed_final_stage_model_request_and_result_cannot_replace_actual_market_frame(tmp_path, monkeypatch, field):
    execution, root, result = _watch_run(tmp_path, monkeypatch)
    _forge_bound_market(root, result, field)
    verify_events(root, execution.run_id, daily_stage_contract(True))
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(root, result, _admission(execution))
    assert error.value.code == "MODEL_REQUIRED_FAILED"


@pytest.mark.parametrize("field", ["close", "as_of", "series_sha256", "id", "symbol"])
def test_public_market_metadata_cannot_be_replaced_after_a_valid_model_call(tmp_path, monkeypatch, field):
    execution, root, result = _watch_run(tmp_path, monkeypatch)
    row = result["raw"]["execution_decisions"][0]["market_evidence"][0]
    row[field] = row[field] + 1 if field == "close" else "forged"
    with pytest.raises(EvidenceInvalid) as error:
        verify_required_model(root, result, _admission(execution))
    assert error.value.code == "MODEL_REQUIRED_FAILED"
