"""EP19: completion, evidence, decision and publication are separate facts."""
from copy import deepcopy
from html import unescape
import importlib
import re

import pytest

from trading_system.data_models import PipelineResult
from trading_system.research_audit import candidate_funnel
from trading_system.report import render_markdown
from trading_system.report_html import render_html


def project(raw=None, **kwargs):
    return importlib.import_module("trading_system.research_status").project_research_status(raw, **kwargs)


def instruction(symbol, action="watch"):
    buy = action == "buy"
    return {"symbol": symbol, "action": action, "target_weight": .02 if buy else 0,
        "action_summary": symbol + " 的原始判断", "why_this": "原始关注理由", "why_now": "原始时点理由",
        "entry_condition": "原始进入条件", "invalidation": "原始失效条件", "risk_note": "原始风险说明",
        "market_evidence": [{"symbol": symbol, "as_of": "2026-11-06", "close": 109.125}],
        "sources": [{"id": "source-" + symbol, "title": symbol + " 原文", "url": "https://example.com/" + symbol,
                     "published_at": "2026-11-06T12:00:00Z"}],
        "entry_price": 101.125 if buy else None, "stop_price": 97.875 if buy else None,
        "shares": 13 if buy else None, "risk_usd": 42.25 if buy else None,
        "time_stop_days": 7 if buy else None, "reference_capital_usd": 100000,
        "valid_for": "2026-11-09T16:00:00-05:00"}


def raw_fixture():
    scan = {"BUY": {"hard_filter": "pass", "selection": "baseline"},
        "HOLD": {"hard_filter": "pass", "selection": "baseline"},
        "GAP": {"hard_filter": "pass", "selection": "baseline"},
        "LATE": {"hard_filter": "pass", "selection": "research_missing"},
        "FILTER": {"hard_filter": "price", "selection": "rejected"},
        "NODATA": {"hard_filter": "no_data", "selection": "rejected"},
        "RISK": {"hard_filter": "pass", "selection": "baseline"}}
    scored = [{"fields": {"ticker": symbol, "entry_template": "A", "s_options": None}}
              for symbol in ("BUY", "HOLD", "GAP", "RISK")]
    risk = {"candidates": [{"ticker": symbol, "status": "accepted", "reasons": []}
                           for symbol in ("BUY", "HOLD", "GAP")] +
                         [{"ticker": "RISK", "status": "rejected", "reasons": ["risk_distance"]}]}
    decisions = [instruction("BUY", "buy"), instruction("HOLD"), instruction("GAP")]
    company = {"contract": "sec-frozen-typed-original/v3", "candidates": {
        symbol: {"status": "required_coverage_missing" if symbol == "GAP" else "filing_verified",
                 "source_ids": [] if symbol == "GAP" else ["source-" + symbol],
                 "missing_cells": ["cell-GAP"] if symbol == "GAP" else [],
                 "required_targets": ["global:macro", "security:us:" + symbol]}
        for symbol in ("BUY", "HOLD", "GAP")}}
    debate = {"company_evidence": company, "execution_decisions": decisions}
    frontier = {"symbols": ["BUY", "HOLD", "GAP", "RISK"], "manifest_sha256": "f" * 64}
    funnel = candidate_funnel(list(scan), scan, scored, risk, debate,
        {"symbols": {symbol: "us:" + symbol for symbol in scan}}, intelligence=frontier)
    gaps = [{"cell_id": "cell-" + symbol, "gap_id": "gap-" + symbol,
             "target_id": "global:macro" if symbol == "macro" else "security:us:" + symbol,
             "target_label": symbol, "required": True, "material_type": "news", "question": "资料问题",
             "status": "missing" if symbol == "GAP" else "present",
             "reason": "no_qualified_material_for_this_question" if symbol == "GAP" else "qualified_material_observed"}
            for symbol in ("macro", "BUY", "HOLD", "GAP", "RISK")]
    return {"run_id": "run-original", "status": "completed", "action": "HOLD",
        "model": {"required": True, "all_required_completed": True}, "execution_decisions": decisions,
        "accounting": {"status": "updated", "execution_blocked": False}, "debate": debate,
        "research_audit": {"funnel": funnel, "stages": [], "rule_coverage": {}},
        "intelligence": {"state": "partial", "gaps": gaps, "frontier": frontier,
            "bundle": {"bundle_id": "bundle-original", "manifest_sha256": "b" * 64, "state": "partial",
                       "missing_required": ["cell-GAP"], "frozen_at": "2026-11-08T00:00:00Z"},
            "pending_candidates": {"candidates": [{"symbol": "LATE", "security_id": "us:LATE",
                "status": "pending_research", "required_work": ["new_market_observation", "required_evidence"],
                "approval_reused": False}]}}}


def verified(raw, *, committed=True):
    pointer = {"run_id": raw["run_id"], "receipt_sha256": "a" * 64, "version_sha256": "c" * 64}
    return {"status": "completed", "pointer": pointer,
        "summary": {"action": raw["action"], "execution_decisions": deepcopy(raw["execution_decisions"]),
                    "disclosures": {"intelligence": deepcopy(raw["intelligence"])}},
        "verification": {"ok": True, "receipt_sha256": pointer["receipt_sha256"],
            "checks": ["admission-policy-date", "frozen-source-files", "ordered-stage-hash-chain",
                "all-stage-and-IO-snapshots", "final-artifacts", "cash-holdings-arithmetic",
                "immutable-account-version", "published-account-ancestry" if committed else "prepared-unpublished-account"]}}


def text(markup):
    markup = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", markup, flags=re.S)
    return unescape(re.sub(r"<[^>]+>", "", markup))


def test_raw_completed_and_updated_account_do_not_prove_receipt():
    value = project(raw_fixture(), observed_state="completed")
    assert value["axes"]["process"]["state"] == "completed"
    assert value["axes"]["evidence"]["state"] == "partial"
    assert value["axes"]["decision"]["state"] == "WAIT_EVIDENCE"
    assert value["axes"]["account"]["state"] == "unverified"
    assert value["verified_receipt"] is False


def test_report_ready_is_prepublication_even_with_completed_graph():
    value = project(raw_fixture(), report_ready=True, work_status={"graph_state": "completed", "units": []})
    assert value["axes"]["process"]["state"] == "report_ready"
    assert value["axes"]["account"]["state"] == "pending_publication"
    assert value["verified_receipt"] is False


def test_verified_receipt_preserves_partial_evidence_and_commits_only_account_axis():
    raw = raw_fixture()
    value = project(raw, verification=verified(raw))
    assert value["axes"]["account"]["state"] == "committed"
    assert value["axes"]["evidence"]["state"] == "partial"
    assert value["axes"]["decision"]["state"] == "WAIT_EVIDENCE"
    assert value["verified_receipt"] is True
    prepared = project(raw, verification=verified(raw, committed=False))
    assert prepared["axes"]["account"]["state"] == "pending_publication"


@pytest.mark.parametrize("mutation", ["ok_false", "missing_checks", "receipt_mismatch", "run_mismatch", "summary_mismatch"])
def test_malformed_verification_cannot_mark_commit(mutation):
    raw = raw_fixture()
    value = verified(raw)
    if mutation == "ok_false": value["verification"]["ok"] = False
    if mutation == "missing_checks": value["verification"]["checks"] = ["published-account-ancestry"]
    if mutation == "receipt_mismatch": value["verification"]["receipt_sha256"] = "0" * 64
    if mutation == "run_mismatch": value["pointer"]["run_id"] = "other-run"
    if mutation == "summary_mismatch": value["summary"]["execution_decisions"][0]["shares"] = 999
    with pytest.raises(ValueError, match="RESEARCH_STATUS_VERIFICATION_INVALID"):
        project(raw, verification=value)


def test_all_funnel_securities_have_distinct_dispositions_and_original_plan():
    raw = raw_fixture()
    original = deepcopy(raw)
    value = project(raw, report_ready=True)
    cards = {row["symbol"]: row for row in value["securities"]}
    assert set(cards) == {"BUY", "HOLD", "GAP", "LATE", "FILTER", "NODATA", "RISK"}
    assert cards["BUY"]["state"] == "BUY" and cards["BUY"]["instruction"] == raw["execution_decisions"][0]
    assert cards["HOLD"]["state"] == "HOLD" and cards["HOLD"]["qualified"] is True
    assert cards["GAP"]["state"] == "WAIT_EVIDENCE" and not cards["GAP"]["qualified"]
    assert cards["LATE"]["state"] == "PENDING_RESEARCH" and not cards["LATE"]["qualified"]
    assert cards["FILTER"]["state"] == "NOT_SELECTED"
    assert cards["NODATA"]["state"] == "WAIT_EVIDENCE"
    assert cards["RISK"]["state"] == "NOT_SELECTED"
    assert value["counts"]["securities"]["qualified_hold"] == 1
    assert value["counts"]["securities"]["wait_evidence"] == 2
    assert raw == original


def test_sufficient_hold_differs_from_wait_without_fabricating_buy_plan():
    raw = raw_fixture()
    raw["intelligence"]["state"] = raw["intelligence"]["bundle"]["state"] = "completed"
    raw["intelligence"]["bundle"]["missing_required"] = []
    for row in raw["intelligence"]["gaps"]: row["status"] = "present"
    value = project(raw, report_ready=True)
    assert value["axes"]["evidence"]["state"] == "sufficient"
    # The original GAP company result remains insufficient even after editing a global count.
    assert next(row for row in value["securities"] if row["symbol"] == "GAP")["state"] == "WAIT_EVIDENCE"
    assert value["axes"]["decision"]["state"] == "WAIT_EVIDENCE"


def test_fully_sufficient_hold_does_not_inherit_an_unresearched_universe_gap():
    raw = raw_fixture()
    raw["intelligence"]["state"] = raw["intelligence"]["bundle"]["state"] = "completed"
    raw["intelligence"]["bundle"]["missing_required"] = []
    for row in raw["intelligence"]["gaps"]: row["status"] = "present"
    company = raw["debate"]["company_evidence"]["candidates"]["GAP"]
    company.update(status="filing_verified", source_ids=["source-GAP"], missing_cells=[])
    for row in raw["research_audit"]["funnel"]["rows"]:
        if row["ticker"] == "GAP":
            row["gates"]["company"] = {"status": "passed", "reasons": []}
            row["gates"]["model"] = {"status": "rejected", "reasons": ["model_watch"]}
    value = project(raw, report_ready=True)
    assert value["axes"]["evidence"]["state"] == "sufficient"
    assert value["axes"]["decision"]["state"] == "qualified"
    assert value["counts"]["securities"]["qualified_hold"] == 2
    assert next(row for row in value["securities"] if row["symbol"] == "NODATA")["state"] == "WAIT_EVIDENCE"


@pytest.mark.parametrize("mutation", ["model_action", "unsafe_buy", "source_conflict", "security_conflict"])
def test_conflicting_frozen_projections_do_not_produce_approved_cards(mutation):
    raw = raw_fixture()
    if mutation == "model_action":
        raw["research_audit"]["funnel"]["rows"][0]["model_action"] = "watch"
    if mutation == "unsafe_buy":
        raw["research_audit"]["funnel"]["rows"][0]["gates"]["numeric_risk"] = {"status": "rejected", "reasons": ["risk"]}
    if mutation == "source_conflict":
        raw["intelligence"]["source_index"] = [{"id": "source-BUY", "title": "different", "url": "https://example.com/BUY"}]
    if mutation == "security_conflict":
        raw["intelligence"]["pending_candidates"]["candidates"][0]["security_id"] = "us:other"
    with pytest.raises(ValueError, match="RESEARCH_STATUS"):
        project(raw)


def test_untrusted_precomputed_status_cannot_upgrade_account_and_no_fake_unit_total():
    raw = raw_fixture()
    raw["research_status"] = {"axes": {"account": {"state": "committed"}}, "verified_receipt": True}
    value = project(raw)
    assert value["axes"]["account"]["state"] == "unverified"
    assert value["counts"]["units"]["total"] is None


def test_missing_unknown_work_state_cannot_be_a_completed_business():
    value = project({}, work_status={"graph_state": "completed", "units": [{"unit_id": "done", "state": "validated"}]})
    assert value["axes"]["process"]["state"] == "work_completed"
    assert value["axes"]["evidence"]["state"] == "unknown"
    assert value["axes"]["account"]["state"] == "unverified"


def test_report_keeps_original_plan_precision_and_never_synthesizes_2r():
    result = PipelineResult("2026-11-08", "fixture", action="HOLD", raw=raw_fixture())
    result.raw["scan_stats"] = {"universe_size": 7, "passed": 5, "selected": 4, "rejected": {"price": 1, "nodata": 1}}
    for renderer in (render_html, render_markdown):
        value = text(renderer(result))
        assert "101.125" in value and "97.875" in value and "42.25" in value and "13 股" in value
        assert "淘汰率" not in value
        assert "2R 保护 107" not in value


def test_empty_static_research_report_retains_unknown_and_real_empty_message():
    result = PipelineResult("2026-11-08", "fixture", raw={"intelligence": {"state": "completed", "gaps": []}})
    value = text(render_html(result))
    assert "状态未知" in value and "本报告没有逐证券记录" in value and "本报告未提供来源目录" in value
    assert "合格观察 1" not in value


def test_empty_and_missing_evidence_stay_unknown():
    value = project({"status": "completed", "execution_decisions": [], "action": "HOLD"})
    assert value["axes"]["evidence"]["state"] == "unknown"
    assert value["axes"]["decision"]["state"] == "unknown"
    assert value["counts"]["documents"]["terminal"] is None
    assert value["securities"] == []


def test_stable_units_count_and_original_document_dispositions_are_separate():
    work = {"graph_state": "running", "plan_revision": 3, "cancel_requested": False,
            "unknown_attempts": 1, "settled_attempts": 2,
            "units": [{"unit_id": "seg-1", "state": "validated"}, {"unit_id": "seg-2", "state": "validated"},
                      {"unit_id": "merge", "state": "running"}]}
    docs = {"admitted": 2, "terminal": 1, "pending": ["doc-2"],
            "documents": {"doc-1": {"state": "eligible"}}}
    value = project({}, work_status=work, document_dispositions=docs, observed_state="running")
    assert value["counts"]["units"]["validated"] == 2
    assert value["counts"]["documents"] == {"admitted": 2, "terminal": 1, "pending": 1,
        "eligible": 1, "irrelevant": 0, "quarantined": 0, "unverifiable": 0, "rejected": 0}
    assert "UNKNOWN_EXTERNAL_ATTEMPT" in value["block_reasons"]
    assert "reconcile_original_attempt_before_resend" in value["resume_conditions"]
    assert value["counts"] == project({}, work_status=work, document_dispositions=docs)["counts"]


@pytest.mark.parametrize("kind", ["unit", "document", "gap", "security"])
def test_conflicting_counts_or_duplicate_identities_fail_closed(kind):
    raw = raw_fixture()
    args = {}
    if kind == "unit": args["work_status"] = {"units": [{"unit_id": "x", "state": "ready"}] * 2}
    if kind == "document": args["document_dispositions"] = {"admitted": 2, "terminal": 2, "pending": [], "documents": {"x": {"state": "eligible"}}}
    if kind == "gap": raw["intelligence"]["gaps"].append(deepcopy(raw["intelligence"]["gaps"][0]))
    if kind == "security": raw["research_audit"]["funnel"]["rows"].append(deepcopy(raw["research_audit"]["funnel"]["rows"][0]))
    with pytest.raises(ValueError, match="RESEARCH_STATUS"):
        project(raw, **args)


def test_observation_cancel_and_scheduler_cancel_do_not_claim_business_or_account_end():
    value = project({}, observed_state="running", observation_state="cancelled",
                    work_status={"graph_state": "running", "cancel_requested": False, "units": []})
    assert value["observation"]["state"] == "cancelled"
    assert value["axes"]["process"]["state"] == "running"
    assert value["axes"]["account"]["state"] == "unverified"
    value = project({}, observed_state="running", work_status={"graph_state": "cancelling",
        "cancel_requested": True, "unknown_attempts": 1, "units": []})
    assert value["axes"]["process"]["state"] == "cancelling"
    assert value["axes"]["decision"]["state"] == "unknown"


def test_source_index_keeps_every_original_and_marks_missing_metadata():
    raw = raw_fixture()
    raw["intelligence"]["source_index"] = [{"id": "unused-original", "title": "未进入交易引用的原文",
        "url": "javascript:alert(1)", "published_at": None, "content_sha256": "e" * 64,
        "completeness": "partial", "source_id": "ir-feed"}]
    value = project(raw)
    assert {row["id"] for row in value["sources"]} == {"unused-original", "source-BUY", "source-HOLD", "source-GAP"}
    assert next(row for row in value["sources"] if row["id"] == "unused-original")["completeness"] == "partial"


def test_report_four_axes_all_securities_and_safe_source_links_without_mutation():
    raw = raw_fixture()
    raw["intelligence"]["source_index"] = [{"id": "unsafe", "title": "<script>source</script>",
        "url": "javascript:alert(1)", "published_at": None}]
    result = PipelineResult("2026-11-08", "fixture", action="HOLD", market_view="原始市场判断", raw=raw)
    original = deepcopy(raw)
    for render in (render_markdown, render_html):
        value = render(result)
        visible = text(value)
        for required in ("研究进度与交付状态", "流程", "证据", "决策", "账户", "报告已生成",
                         "等待账户发布核验", "等待证据", "合格观察", "待补查", "LATE", "NODATA",
                         "FILTER", "RISK", "完整来源目录", "原始失效条件", "run-original"):
            assert required in visible
        assert "账户已提交" not in visible
        assert 'href="javascript:' not in value and "<script>source</script>" not in value
        assert "淘汰率" not in visible, "未研究候选不能并入研究后淘汰率"
    assert raw == original


def test_static_report_has_real_filter_empty_state_and_no_paid_or_cancel_dispatch():
    result = PipelineResult("2026-11-08", "fixture", action="HOLD", raw=raw_fixture())
    markup = render_html(result)
    for expected in ("research-security-search", "research-security-filter", "research-security-empty",
                     "filterResearchSecurities", "aria-live='polite'", "封存报告", "不会启动研究"):
        assert expected in markup
    assert "fetch(" not in markup and "XMLHttpRequest" not in markup
    assert "取消调度" not in markup or "不能取消调度" in markup


def test_default_status_summary_is_compact_and_cards_are_cursor_paginated():
    raw = raw_fixture()
    summary = project(raw, detail="summary", limit=2)
    assert "securities" not in summary and "sources" not in summary
    first = project(raw, detail="cards", limit=2)
    assert len(first["securities"]) == 2 and first["next_cursor"]
    second = project(raw, detail="cards", limit=2, cursor=first["next_cursor"])
    assert second["securities"]
    assert not ({row["symbol"] for row in first["securities"]}
                & {row["symbol"] for row in second["securities"]})


def test_source_unresolved_is_a_partial_required_gap_not_clean_evidence():
    raw = raw_fixture()
    raw["intelligence"]["gaps"][1]["status"] = "source_unresolved"
    value = project(raw, detail="summary")
    assert value["axes"]["evidence"]["state"] == "partial"
    assert value["axes"]["decision"]["state"] == "WAIT_EVIDENCE"
