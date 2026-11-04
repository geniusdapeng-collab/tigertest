"""日度归因（归因分析师）— 对 journal 已落账记录逐笔归因 + 违规六条检测。

口径（白皮书附录D + UPGRADE_PLAN_v3 §5）：
  - 只复盘流程不复盘运气：归因只回答"这笔交易从哪来、怎么进、怎么出、
    当时市场环境是什么"，不重构决策、不评判单次盈亏的运气成分；
  - 归因输入仅限【已落账数据】（journal 记录）与【当日快照】
    （调用方传入的 market snapshot，如禁追高标记/财报日历），不重新评分；
  - 违规六条命中即标记并计数，进复盘纪要与治理事件。

违规六条（附录D）：
  V1 MRS不允许却开仓     mrs_star < MRS_GATE_BLOCK(4.0) 仍入场
  V2 非主线交易          无主线板块归属且无产业链归属仍入场
  V3 禁追高条件下追高    当日快照标记禁追高（广度<40 仅权重拉动）仍入场
  V4 财报前未降仓        当日快照财报名单内标的仍以标准仓入场（未 ×0.5/回避）
  V5 触发止损不执行      持仓浮动 R ≤ -1R（已穿止损）仍未平仓
  V6 ≥2R未保护          浮动/已实现 ≥2R 但未启动盈利保护
"""

from __future__ import annotations

import math

from .. import config

# 违规六条代码表（附录D 顺序固定，报告与治理事件共用同一口径）
VIOLATIONS: dict[str, str] = {
    "V1": "MRS不允许却开仓",
    "V2": "非主线交易",
    "V3": "禁追高条件下追高",
    "V4": "财报前未降仓",
    "V5": "触发止损不执行",
    "V6": "≥2R未保护",
}

# 出场类型归一（exit_engine note → 附录D 出场分类）
_EXIT_TYPES = (
    ("保护性止盈", "盈利保护"),
    ("止损离场", "结构止损"),
    ("时间止损", "时间止损"),
    ("作废", "作废"),
)


def mrs_band(mrs_star) -> str:
    """MRS* 区间归属（信号来源层的环境维）。"""
    if mrs_star is None:
        return "未知"
    m = float(mrs_star)
    if m >= 8.0:
        return "强共振(≥8)"
    if m >= config.OPEN_LONG["mrs"]:
        return "可交易(6-8)"
    if m >= config.MRS_GATE_BLOCK:
        return "轻仓区(4-6)"
    return "禁开仓(<4)"


def exit_type(rec: dict) -> str:
    """出场类型：结构止损 / 时间止损 / 盈利保护 / 到期 / 作废 / 持仓中 / 待成交。"""
    status = rec.get("status")
    if status == "void":
        return "作废"
    if status == "pending":
        return "待成交"
    if status == "open":
        return "持仓中" if rec.get("entry") is not None else "待成交"
    note = rec.get("note") or ""
    for key, label in _EXIT_TYPES:
        if key in note:
            return label
    return "到期" if rec.get("exit_date") else "未知"


def _entered(rec: dict) -> bool:
    """是否真实入场（closed 或 open 且已有入场价）。"""
    if rec.get("accounting_basis") == "legacy_hypothesis":
        return False
    if rec.get("accounting_basis") == "paper_execution":
        return bool(rec.get("paper_event_ids", {}).get("filled") and rec.get("entry_date")
                    and rec.get("status") in ("closed", "open") and rec.get("entry") is not None)
    return rec.get("status") in ("closed", "open") and rec.get("entry") is not None


def detect_violations(rec: dict, snapshot: dict | None = None) -> list[str]:
    """对单条落账记录做违规六条检测，返回命中的违规代码列表。

    snapshot：当日市场快照（可选），支持键：
      no_chase: bool            当日禁追高（广度<40% 且仅权重拉动）
      earnings: list[str]       当日 1-2 天内财报的标的名单
    无快照时 V3/V4 不可判定（宁缺不猜——只基于已落账数据与当日快照）。
    """
    hits: list[str] = []
    if not _entered(rec):
        return hits                                   # 作废/未入场不判违规
    snap = snapshot or {}

    mrs_star = rec.get("mrs_star")
    if mrs_star is not None and float(mrs_star) < config.MRS_GATE_BLOCK:
        hits.append("V1")
    if snap.get("mainline_eligible") is False or (
            "mainline_eligible" not in snap and not rec.get("sector") and not rec.get("chain")):
        hits.append("V2")
    paper = rec.get("accounting_basis") == "paper_execution"
    if snap.get("no_chase") and (not paper or (
            _finite(rec.get("entry_raw")) and _finite(snap.get("entry_ceiling"))
            and rec["entry_raw"] > snap["entry_ceiling"])):
        hits.append("V3")
    if rec.get("ticker") in (snap.get("earnings") or []) \
            and rec.get("mode") == "标准做多" and (not paper or (
                _finite(snap.get("earnings_size_factor")) and _finite(snap.get("earnings_size_limit"))
                and snap["earnings_size_factor"] > snap["earnings_size_limit"])):
        hits.append("V4")
    r_live = rec.get("r_live")
    if rec.get("status") == "open" and r_live is not None and float(r_live) <= -1.0:
        hits.append("V5")
    if rec.get("protected") is False:
        live = float(r_live) if r_live is not None else float(rec.get("r") or 0.0)
        if rec.get("status") == "closed" and rec.get("r") is not None:
            live = float(rec["r"])
        if live >= config.PROFIT_PROTECT_R:
            hits.append("V6")
    return hits


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _rule_inputs(rec, snap):
    """Record the actual applicability and inputs without treating absence as a pass."""
    if not _entered(rec):
        return {code: ("not_applicable", []) for code in VIOLATIONS}
    status = rec.get("status")
    evidence_r = rec.get("r") if status == "closed" else rec.get("r_live")
    paper = rec.get("accounting_basis") == "paper_execution"
    def verdict(ok, *fields):
        return ("checked", list(fields)) if ok else ("insufficient_evidence", [])
    chase_fields = ["no_chase"]
    chase_known = type(snap.get("no_chase")) is bool
    if paper and snap.get("no_chase") is True:
        chase_fields.extend(("entry_raw", "entry_ceiling"))
        chase_known = _finite(rec.get("entry_raw")) and _finite(snap.get("entry_ceiling"))
    earnings_fields = ["mode", "earnings"]
    earnings = snap.get("earnings")
    earnings_known = rec.get("mode") == "标准做多" and isinstance(earnings, list)
    if paper and earnings_known and rec.get("ticker") in earnings:
        earnings_fields.extend(("earnings_size_factor", "earnings_size_limit"))
        earnings_known = _finite(snap.get("earnings_size_factor")) and _finite(snap.get("earnings_size_limit"))
    return {
        "V1": verdict(_finite(rec.get("mrs_star")), "mrs_star"),
        "V2": verdict(type(snap.get("mainline_eligible")) is bool, "mainline_eligible"),
        "V3": verdict(chase_known, *chase_fields),
        "V4": (("not_applicable", ["mode"]) if rec.get("mode") and rec["mode"] != "标准做多"
               else verdict(earnings_known, *earnings_fields)),
        "V5": (("not_applicable", ["status"]) if status != "open"
               else verdict(_finite(rec.get("r_live")), "r_live")),
        "V6": verdict(_finite(evidence_r) and (evidence_r < config.PROFIT_PROTECT_R
                     or type(rec.get("protected")) is bool),
                     *(["r" if status == "closed" else "r_live", "protected"]
                       if type(rec.get("protected")) is bool else ["r" if status == "closed" else "r_live"])),
    }


def attribute_record(rec: dict, snapshot: dict | None = None) -> dict:
    """单条归因记录：信号来源层 / 入场质量 / 出场类型 / 市场环境快照 / 违规标记。"""
    entry, entry_ref = rec.get("entry"), rec.get("entry_ref")
    slippage = (round(float(entry) - float(entry_ref), 4)
                if entry is not None and entry_ref else None)
    violations = detect_violations(rec, snapshot)
    return {
        "date": rec.get("date"), "ticker": rec.get("ticker"),
        "status": rec.get("status"),
        "accounting_basis": rec.get("accounting_basis"),
        # 信号来源层
        "signal_layer": {
            "mrs_band": mrs_band(rec.get("mrs_star")),
            "sector": rec.get("sector") or "（无主线归属）",
            "chain": rec.get("chain") or "（无链归属）",
            "template": rec.get("template") or "无",
            "mode": rec.get("mode"),
            "tss_final": rec.get("tss_final"),
        },
        # 入场质量
        "entry_quality": {
            "trigger": "次日开盘" if _entered(rec) else "未入场",
            "entry_ref": entry_ref, "entry": entry, "slippage": slippage,
        },
        # 出场类型
        "exit_type": exit_type(rec),
        "exit": rec.get("exit"), "exit_date": rec.get("exit_date"),
        "r": rec.get("r"), "r_live": rec.get("r_live"),
        # 市场环境快照（落账时记录，不重构）
        "market_env": {"mrs_star": rec.get("mrs_star"), "action": rec.get("action")},
        # 违规标记
        "violations": violations,
        "violation_labels": [VIOLATIONS[v] for v in violations],
    }


def attribute_journal(records: list[dict],
                      snapshots: dict[str, dict] | None = None) -> dict:
    """批量归因：对 journal 记录逐笔归因并汇总违规计数。

    snapshots: {signal_date: snapshot}（按信号日取当日快照）。
    返回 {attributions, violation_counts, flagged}。
    """
    snapshots = snapshots or {}
    attributions = []
    evidence = {code: [] for code in VIOLATIONS}
    authoritative = any(rec.get("accounting_basis") == "paper_execution" for rec in records)
    for rec in records:
        snapshot = dict(snapshots.get(rec.get("date")) or {})
        snapshot.update(rec.get("review_snapshot") or {})
        assessed = _rule_inputs(rec, snapshot)
        attribution = attribute_record(rec, snapshot)
        if authoritative:
            attribution["violations"] = [code for code in attribution["violations"] if assessed[code][0] == "checked"]
            attribution["violation_labels"] = [VIOLATIONS[code] for code in attribution["violations"]]
        attributions.append(attribution)
        for code, (status, fields) in assessed.items():
            item = {"signal_id": rec.get("signal_id"), "date": rec.get("date"),
                    "ticker": rec.get("ticker"), "status": status, "fields": fields}
            if rec.get("paper_event_ids") and status != "not_applicable":
                item["paper_event_ids"] = rec["paper_event_ids"]
            evidence[code].append(item)
    counts = {code: 0 for code in VIOLATIONS}
    flagged = []
    for a in attributions:
        for v in a["violations"]:
            counts[v] += 1
        if a["violations"]:
            flagged.append({"date": a["date"], "ticker": a["ticker"],
                            "violations": a["violations"],
                            "labels": a["violation_labels"]})
    coverage = {}
    for code, items in evidence.items():
        checked = sum(item["status"] == "checked" for item in items)
        missing = sum(item["status"] == "insufficient_evidence" for item in items)
        coverage[code] = {"status": ("insufficient_evidence" if missing else
                                     "checked" if checked else "not_applicable"),
                          "denominator": checked + missing, "checked": checked,
                          "insufficient_evidence": missing, "not_applicable": len(items) - checked - missing,
                          "violations": sum(code in item["violations"] and detail["status"] == "checked"
                                            for item, detail in zip(attributions, items)) if checked else None,
                          "evidence": items}
    return {"attributions": attributions, "violation_counts": counts, "violation_coverage": coverage,
            "flagged": flagged,
            "total_violations": sum(counts.values())}
