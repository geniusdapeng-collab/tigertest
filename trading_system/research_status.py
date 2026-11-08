"""Pure, conservative projections of already observed research records.

This module performs no IO, retries, scoring or publication. ``verification``
is the complete return value of a successful ``agent_runtime.verify_run`` call,
never a receipt read directly from disk. The caller owns that trust boundary;
the shape/binding checks here prevent accidental mixing of different runs.
Report rendering deliberately omits that argument: a report generated before
publication cannot attest to the later account compare-and-swap.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re


CONTRACT = "tiger.research-status/v1"
AXIS_LABELS = {"process": "流程", "evidence": "证据", "decision": "决策", "account": "账户"}
STATE_LABELS = {
    "unknown": "状态未知", "accepted": "已受理", "ready": "等待调度", "running": "运行中",
    "work_completed": "研究工作已结束，等待业务收尾", "report_ready": "报告已生成",
    "completed": "流程已结束", "failed": "流程失败", "cancelling": "停止调度，待核对在途调用",
    "cancelled": "调度已取消", "partial": "必需证据仍有缺口", "sufficient": "必需证据充分",
    "qualified": "已形成合格决策", "WAIT_EVIDENCE": "等待证据",
    "unverified": "尚未核验账户提交", "pending_publication": "等待账户发布核验",
    "committed": "账户已提交", "blocked": "账户动作受阻",
    "BUY": "纸面买入计划", "HOLD": "合格观察", "PENDING_RESEARCH": "待补查",
    "NOT_SELECTED": "本轮未入选", "NOT_ASSESSED": "尚未完成判断",
    "not_researched": "尚未完成研究",
    "connected": "观察正常", "disconnected": "观察中断", "loading": "正在读取状态",
}
GATE_LABELS = {"market_data": "行情可用", "hard_filter": "基础硬筛", "research": "冻结研究范围",
    "entry_setup": "入场形态", "numeric_risk": "数值风险", "portfolio": "组合批准",
    "company": "公司证据", "model": "模型终审"}
GATE_STATES = {"passed": "通过", "rejected": "未通过", "unknown": "未知", "not_applicable": "不适用"}
_UNITS = ("ready", "blocked", "running", "validated", "failed", "unknown", "cancelled")
_DISPOSITIONS = ("eligible", "irrelevant", "quarantined", "unverifiable", "rejected")
_VERIFIED_CHECKS = {"admission-policy-date", "frozen-source-files", "ordered-stage-hash-chain",
    "all-stage-and-IO-snapshots", "final-artifacts", "cash-holdings-arithmetic", "immutable-account-version"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def state_label(state):
    return STATE_LABELS.get(state, str(state))


def _mapping(value):
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError("RESEARCH_STATUS_OBJECT_INVALID")
    return value


def _rows(value):
    if value is None:
        return []
    if type(value) is not list or any(type(row) is not dict for row in value):
        raise ValueError("RESEARCH_STATUS_ROWS_INVALID")
    return value


def _index(rows, key):
    result = {}
    for row in _rows(rows):
        identity = row.get(key)
        if type(identity) is not str or not identity or identity in result:
            raise ValueError("RESEARCH_STATUS_IDENTITY_INVALID")
        result[identity] = row
    return result


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("RESEARCH_STATUS_COUNT_INVALID")
    return value


def _verified(raw, verification, run_id):
    if verification is None:
        return None
    try:
        check = verification["verification"]
        pointer = verification["pointer"]
        summary = verification["summary"]
        checks = check["checks"]
        if (type(checks) is not list or any(type(item) is not str for item in checks)
                or len(checks) != len(set(checks)) or not _VERIFIED_CHECKS <= set(checks)
                or check["ok"] is not True or verification["status"] not in {"completed", "completed_degraded"}
                or not _HASH.fullmatch(pointer["receipt_sha256"])
                or not _HASH.fullmatch(pointer["version_sha256"])
                or check["receipt_sha256"] != pointer["receipt_sha256"]
                or type(pointer["run_id"]) is not str or not pointer["run_id"]
                or run_id is not None and run_id != pointer["run_id"]
                or len(set(checks) & {"published-account-ancestry", "prepared-unpublished-account"}) != 1):
            raise ValueError
        for key in ("execution_decisions", "action"):
            if key in raw and raw[key] != summary.get(key):
                raise ValueError
        if "intelligence" in raw and raw["intelligence"] != summary["disclosures"].get("intelligence"):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("RESEARCH_STATUS_VERIFICATION_INVALID") from None
    return verification


def _document_counts(value):
    if value is None:
        return {key: None for key in ("admitted", "terminal", "pending", *_DISPOSITIONS)}
    value = _mapping(value)
    documents = _mapping(value.get("documents"))
    pending = value.get("pending")
    if (type(pending) is not list or any(type(key) is not str or not key for key in pending)
            or len(pending) != len(set(pending)) or set(pending) & documents.keys()
            or any(type(key) is not str or not key for key in documents)
            or _count(value.get("terminal")) != len(documents)
            or _count(value.get("admitted")) != len(documents) + len(pending)
            or any(type(row) is not dict or row.get("state") not in _DISPOSITIONS for row in documents.values())):
        raise ValueError("RESEARCH_STATUS_DOCUMENT_COUNTS_INVALID")
    counts = Counter(row["state"] for row in documents.values())
    result = {"admitted": value["admitted"], "terminal": len(documents), "pending": len(pending),
              **{key: counts[key] for key in _DISPOSITIONS}}
    if any(key in value and _count(value[key]) != result[key] for key in _DISPOSITIONS):
        raise ValueError("RESEARCH_STATUS_DOCUMENT_COUNTS_INVALID")
    return result


def _source_index(raw, instructions, rows):
    intelligence = _mapping(raw.get("intelligence"))
    sources = {key: deepcopy(row) for key, row in _index(intelligence.get("source_index"), "id").items()}
    for item in instructions.values():
        for source in _rows(item.get("sources")):
            identity = source.get("id")
            if type(identity) is not str or not identity:
                raise ValueError("RESEARCH_STATUS_SOURCE_INVALID")
            previous = sources.get(identity, {})
            if any(key in previous and key in source and previous[key] != source[key]
                   for key in ("title", "url", "published_at", "content_sha256")):
                raise ValueError("RESEARCH_STATUS_SOURCE_CONFLICT")
            sources[identity] = {**deepcopy(source), **previous}
    for row in rows:
        for identity in row["source_ids"]:
            sources.setdefault(identity, {"id": identity, "metadata_state": "not_in_report"})
    return [sources[key] for key in sorted(sources)]


def _security_rows(raw, gaps, model_completed):
    intelligence = _mapping(raw.get("intelligence"))
    funnel = _mapping(_mapping(raw.get("research_audit")).get("funnel"))
    original = _index(funnel.get("rows"), "ticker")
    if "universe_size" in funnel and _count(funnel["universe_size"]) != len(original):
        raise ValueError("RESEARCH_STATUS_SECURITY_COUNTS_INVALID")
    instructions = _index(raw.get("execution_decisions"), "symbol")
    pending = _index(_mapping(intelligence.get("pending_candidates")).get("candidates"), "symbol")
    company = _mapping(_mapping(_mapping(raw.get("debate")).get("company_evidence")).get("candidates"))
    frontier = set(_mapping(intelligence.get("frontier")).get("symbols", []))
    symbols = list(original)
    symbols.extend(symbol for symbol in (*instructions, *pending) if symbol not in symbols)
    cards = []
    for symbol in symbols:
        row, instruction, candidate = original.get(symbol, {}), instructions.get(symbol), pending.get(symbol)
        if instruction and row.get("model_action") is not None and row["model_action"] != instruction.get("action"):
            raise ValueError("RESEARCH_STATUS_DECISION_CONFLICT")
        security_id = row.get("security_id") or (candidate or {}).get("security_id")
        if candidate and row.get("security_id") and candidate.get("security_id") != row["security_id"]:
            raise ValueError("RESEARCH_STATUS_SECURITY_IDENTITY_CONFLICT")
        gates = _mapping(row.get("gates"))
        for gate in gates.values():
            if (type(gate) is not dict or gate.get("status") not in GATE_STATES
                    or type(gate.get("reasons")) is not list
                    or any(type(reason) is not str for reason in gate["reasons"])):
                raise ValueError("RESEARCH_STATUS_GATE_INVALID")
        outcome = _mapping(company.get(symbol))
        targets = outcome.get("required_targets", ["global:macro", "security:" + security_id] if security_id else ["global:macro"])
        if type(targets) is not list or any(type(target) is not str for target in targets):
            raise ValueError("RESEARCH_STATUS_TARGETS_INVALID")
        required = [gap for gap in gaps if gap.get("required") is True and
                    (gap.get("target_id") in targets or str(gap.get("target_id", "")).startswith("account:"))]
        missing = [gap for gap in required if gap.get("status") != "present"]
        reasons = sorted({reason for gate in gates.values() for reason in gate["reasons"]
                          if gate["status"] in {"unknown", "rejected"}})
        reasons += ["required_evidence_missing:" + gap["cell_id"] for gap in missing]
        outside = row.get("selection") == "research_missing" or candidate is not None
        unknown = any(gate["status"] == "unknown" for gate in gates.values())
        rejected = any(gate["status"] == "rejected" for name, gate in gates.items() if name != "model")
        company_ok = outcome.get("status") == "filing_verified" or gates.get("company", {}).get("status") == "passed"
        company_gap = bool(outcome) and outcome.get("status") != "filing_verified"
        scope_sufficient = bool(required) and not missing and (company_ok or intelligence.get("state") == "completed")
        if instruction and instruction.get("action") == "buy" and (outside or missing or unknown or rejected or company_gap
                or any(gates.get(key, {}).get("status") != "passed" for key in
                       ("market_data", "hard_filter", "numeric_risk", "portfolio", "company", "model"))):
            raise ValueError("RESEARCH_STATUS_BUY_GATE_CONFLICT")
        qualified = False
        if outside:
            state, evidence = "PENDING_RESEARCH", "not_researched"
            reasons = sorted(set([*reasons, "outside_frozen_research_frontier"]))
        elif missing or unknown or company_gap:
            state, evidence = "WAIT_EVIDENCE", "partial"
        elif rejected:
            state, evidence = "NOT_SELECTED", "sufficient" if scope_sufficient else "unknown"
        elif instruction and model_completed and company_ok and scope_sufficient:
            if instruction.get("action") not in {"buy", "watch"}:
                raise ValueError("RESEARCH_STATUS_ACTION_INVALID")
            state, evidence, qualified = ("BUY" if instruction["action"] == "buy" else "HOLD"), "sufficient", True
        else:
            state, evidence = "NOT_ASSESSED", "unknown"
        ids = sorted(set(row.get("company_source_ids", []) + outcome.get("source_ids", []) +
                         [source["id"] for source in _rows((instruction or {}).get("sources"))]))
        cards.append({"symbol": symbol, "security_id": security_id, "state": state,
            "qualified": qualified, "evidence_state": evidence, "selection": row.get("selection"),
            "in_frozen_frontier": symbol in frontier,
            "gates": deepcopy(gates), "block_reasons": reasons, "missing_required": deepcopy(missing),
            "source_ids": ids, "instruction": deepcopy(instruction),
            "required_work": deepcopy((candidate or {}).get("required_work", [])),
            "original_decision_unchanged": True})
    return cards, instructions


def project_research_status(raw=None, *, work_status=None, document_dispositions=None,
                            verification=None, observed_state=None, report_ready=False,
                            observation_state="connected", run_id=None):
    """Return four independent axes, stable identity counts and full security rows.

    Missing inputs stay unknown. Unit counts are not document counts. The
    document argument must be the original-document disposition projection,
    not a number of chunks, provider attempts or model responses. No timestamp
    is generated here, so repeated reads of identical observations are stable.
    """
    raw, work = _mapping(raw), _mapping(work_status)
    run_id = run_id or raw.get("run_id")
    checked = _verified(raw, verification, run_id)
    if checked:
        summary = checked["summary"]
        raw = {**raw, "action": summary["action"],
               "execution_decisions": summary.get("execution_decisions", []),
               "intelligence": summary.get("disclosures", {}).get("intelligence", {})}
        run_id = checked["pointer"]["run_id"]
    intelligence = _mapping(raw.get("intelligence"))
    gaps = list(_index(intelligence.get("gaps"), "cell_id").values())
    if any(type(row.get("required")) is not bool or row.get("status") not in
           {"present", "missing", "conflicting", "unverifiable", "not_connected", "pending"} for row in gaps):
        raise ValueError("RESEARCH_STATUS_GAP_INVALID")
    required = [row for row in gaps if row["required"]]
    missing = [row for row in required if row["status"] != "present"]
    bundle = _mapping(intelligence.get("bundle"))
    if (missing or intelligence.get("state") == "partial" or bundle.get("state") == "partial"
            or bundle.get("missing_required")):
        evidence_state = "partial"
    elif required and intelligence.get("state") == "completed":
        evidence_state = "sufficient"
    else:
        evidence_state = "unknown"
    model = _mapping(raw.get("model"))
    model_completed = (model.get("required") is True and model.get("all_required_completed") is True) or bool(
        checked and checked["summary"].get("model"))
    cards, instructions = _security_rows(raw, gaps, model_completed)
    security_counts = Counter(row["state"] for row in cards)
    units = _index(work.get("units"), "unit_id")
    if any(row.get("state") not in _UNITS for row in units.values()):
        raise ValueError("RESEARCH_STATUS_UNIT_STATE_INVALID")
    unit_counts = Counter(row["state"] for row in units.values())
    unknown_attempts = _count(work.get("unknown_attempts", 0))
    cancel_requested = work.get("cancel_requested", False)
    if type(cancel_requested) is not bool or type(report_ready) is not bool:
        raise ValueError("RESEARCH_STATUS_FLAG_INVALID")
    if observation_state not in {"connected", "disconnected", "cancelled", "loading"}:
        raise ValueError("RESEARCH_STATUS_OBSERVATION_INVALID")
    graph_state = work.get("graph_state")
    state = observed_state or "unknown"
    if checked:
        process_state = "completed"
    elif cancel_requested:
        process_state = "cancelling" if unknown_attempts or graph_state == "cancelling" else "cancelled"
    elif state in {"failed", "cancelled"}:
        process_state = state
    elif report_ready:
        process_state = "report_ready"
    elif state in {"completed", "completed_degraded"}:
        process_state = "completed"
    elif state in {"accepted", "ready", "running"}:
        process_state = state
    else:
        process_state = {"completed": "work_completed", "failed": "failed", "running": "running",
                         "ready": "ready", "cancelling": "cancelling", "cancelled": "cancelled"}.get(graph_state, "unknown")
    if evidence_state == "partial" or any(row["state"] == "WAIT_EVIDENCE" and row["in_frozen_frontier"] for row in cards):
        decision_state = "WAIT_EVIDENCE"
    elif evidence_state == "sufficient" and model_completed:
        decision_state = "qualified"
    else:
        decision_state = "unknown"
    if checked and "published-account-ancestry" in checked["verification"]["checks"]:
        account_state = "committed"
    elif checked or report_ready:
        account_state = "pending_publication"
    elif any(_mapping(raw.get(key)).get("execution_blocked") is True for key in ("accounting", "currency_policy")):
        account_state = "blocked"
    else:
        account_state = "unverified"
    reasons, resume = [], []
    if decision_state == "WAIT_EVIDENCE":
        reasons.append("REQUIRED_EVIDENCE_MISSING")
        resume.append("obtain_qualified_originals_and_repeat_required_checks")
    if security_counts["PENDING_RESEARCH"]:
        reasons.append("PENDING_RESEARCH_OUTSIDE_FRONTIER")
        resume.append("new_explicit_research_admission_with_fresh_market_and_account_checks")
    if unknown_attempts or unit_counts["unknown"]:
        reasons.append("UNKNOWN_EXTERNAL_ATTEMPT")
        resume.append("reconcile_original_attempt_before_resend")
    if unit_counts["failed"] or process_state == "failed":
        reasons.append("WORK_FAILED")
        resume.append("resolve_recorded_failure_with_original_run_identity")
    if cancel_requested:
        reasons.append("SCHEDULER_CANCEL_REQUESTED")
        resume.append("reconcile_inflight_attempts_before_any_new_dispatch")
    if observation_state in {"disconnected", "cancelled"}:
        reasons.append("OBSERVATION_" + observation_state.upper())
        resume.append("read_same_run_status_without_starting_new_research")
    if account_state != "committed":
        reasons.append("ACCOUNT_PUBLICATION_UNVERIFIED")
    return {"schema": CONTRACT, "run_id": run_id, "verified_receipt": checked is not None,
        "basis": "verified_run" if checked else "report_snapshot" if report_ready else "observed_records",
        "axes": {"process": {"state": process_state, "observed_state": observed_state, "graph_state": graph_state},
            "evidence": {"state": evidence_state, "scope": "frozen_research_frontier",
                         "required": len(required), "present": len(required) - len(missing), "missing": len(missing)},
            "decision": {"state": decision_state, "recorded_action": raw.get("action"),
                         "qualified_securities": sum(row["qualified"] for row in cards)},
            "account": {"state": account_state, "pointer": deepcopy(checked["pointer"]) if checked else None}},
        "observation": {"state": observation_state, "cancelling_observation_stops_scheduler": False},
        "scheduling": {"cancel_requested": cancel_requested, "unknown_attempts": unknown_attempts},
        "counts": {"units": {"total": len(units) if work_status is not None else None,
                             **{key: unit_counts[key] if work_status is not None else None for key in _UNITS}},
            "documents": _document_counts(document_dispositions),
            "securities": {"total": len(cards), "buy": security_counts["BUY"], "qualified_hold": security_counts["HOLD"],
                "wait_evidence": security_counts["WAIT_EVIDENCE"], "pending_research": security_counts["PENDING_RESEARCH"],
                "not_selected": security_counts["NOT_SELECTED"], "not_assessed": security_counts["NOT_ASSESSED"]}},
        "block_reasons": sorted(set(reasons)), "resume_conditions": sorted(set(resume)),
        "securities": cards, "sources": _source_index(raw, instructions, cards),
        "missing_required": deepcopy(missing), "frozen_bundle": deepcopy(bundle),
        "plan_revision": work.get("plan_revision"),
        "usage": {"known": deepcopy(work.get("known_usage")),
                  "observed": deepcopy(work.get("known_usage_observed")), "unknown_attempts": unknown_attempts}}
