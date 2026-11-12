"""TechChainFusionAgent — 子集群汇总 + 标准化接口（用户指令三·融合机制）。

输出 TechChainSignal（固定格式）汇入主决策引擎：
  - prosperity：产业链景气度评分 0..10
      = 行情动能 0.40 + 全球联动 0.25 + 舆情 0.20 + 风险 0.15（扣分项反向）
      缺失维度剔除再归一化（与全系统 v4.1 校准原则一致）；
  - risk_level / alerts：风险预警（risk_level ≥ 8 → 主引擎强制压制链加成）；
  - transmission：影响传导图 {from_link: {to_link: strength 0..1}}，
      由相邻环节动量差构造（上游动量显著高于中游 = 正向传导中游）；
  - bonus_hint：给 ChainCycleAgent 的乘性加成建议（0.90..1.15）。

fusion 是确定性合成（规则保留区：权重固定、无歧义）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..redline import ExecutionTracer, Passthrough
from .agents import (ChainMonitorRow, GlobalLinkageRow, RiskAlert, SentimentRow)
from .universe import SUBCHAIN_TO_MAIN, TECH_SUBCHAINS

log = logging.getLogger("tech_chain.fusion")

W_MOM, W_GLOBAL, W_SENT, W_RISK = 0.40, 0.25, 0.20, 0.15


@dataclass
class TechChainSignal:
    """汇入主决策引擎的标准化信号。"""
    chain_id: str
    main_chain: str               # 映射到 chains.py 的主链
    prosperity: float | None      # 0..10（None=全部维度缺失）
    risk_level: float | None      # 0..10（有证据且无预警=0，证据不足=None）
    alerts: list[dict] = field(default_factory=list)
    transmission: dict[str, dict[str, float]] = field(default_factory=dict)
    bonus_hint: float = 1.0       # 0.90..1.15，乘到 TSS chain_bonus 上
    leading_link: str | None = None
    degraded_components: list[str] = field(default_factory=list)  # 透传环节名
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id, "main_chain": self.main_chain,
            "prosperity": self.prosperity, "risk_level": self.risk_level,
            "alerts": self.alerts, "transmission": self.transmission,
            "bonus_hint": round(self.bonus_hint, 3),
            "leading_link": self.leading_link,
            "degraded_components": self.degraded_components,
            "evidence": self.evidence[:6],
        }


@dataclass
class StructuredTechChainSignal(TechChainSignal):
    """V2 keeps an incomplete assessment separate from known severe alerts."""
    known_alert_level: float | None = None
    risk_assessment_status: str = "insufficient_evidence"
    risk_evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**super().to_dict(), "known_alert_level": self.known_alert_level,
                "risk_assessment_status": self.risk_assessment_status,
                "risk_evidence_ids": list(self.risk_evidence_ids)}


def _score_momentum(row: ChainMonitorRow) -> float | None:
    """链 20 日动量 → 0..10。-10%→0 分，0→5 分，+10%→10 分（线性截断）。"""
    if row.chain_mom20 is None:
        return None
    return max(0.0, min(10.0, 5.0 + row.chain_mom20 * 50.0))


def _score_global(cid: str, linkage: list[GlobalLinkageRow]) -> float | None:
    """该链全球龙头的 20 日相对强度 → 0..10。"""
    leaders = TECH_SUBCHAINS[cid]["global_leaders"]
    vals = [r.rs_vs_spy for r in linkage if r.ticker in leaders and r.rs_vs_spy is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return max(0.0, min(10.0, 5.0 + avg * 80.0))


def _score_sentiment(row: SentimentRow | None) -> float | None:
    if row is None or row.degraded or row.sentiment_score is None:
        return None
    heat = row.heat if row.heat is not None else 5.0
    # 情感方向 × 热度加权：强情感+高热度=极值，高热度+中性=中性
    return max(0.0, min(10.0, 5.0 + row.sentiment_score * (heat / 10.0) * 5.0))


def _risk_penalty(cid: str, alerts: list[RiskAlert]) -> tuple[float | None, float]:
    """返回 (风险维度得分, 原始风险等级)。无预警=满分 10（不扣分）。"""
    mine = [a for a in alerts if a.chain_id == cid or a.chain_id == "unknown"]
    if not mine:
        return 10.0, 0.0
    top = max(a.severity for a in mine)
    return max(0.0, 10.0 - top), top


def _transmission_graph(row: ChainMonitorRow) -> dict[str, dict[str, float]]:
    """相邻环节动量差 → 传导强度 0..1（上游强、中游未动=传导中游进行中）。"""
    order = ["upstream", "midstream", "downstream"]
    moms = {l.link: l.mom20 for l in row.links if l.mom20 is not None}
    graph: dict[str, dict[str, float]] = {}
    for a, b in zip(order, order[1:]):
        if a in moms and b in moms:
            diff = moms[a] - moms[b]
            strength = max(0.0, min(1.0, 0.5 + diff * 10.0))
            graph[a] = {b: round(strength, 3)}
    return graph


class TechChainFusionAgent:
    """tech.fusion（rules）：合成六链信号。"""

    def execute(self,
                monitor: list[ChainMonitorRow],
                linkage: list[GlobalLinkageRow],
                sentiment: dict[str, SentimentRow] | Passthrough,
                alerts: list[RiskAlert] | Passthrough,
                tracer: ExecutionTracer | None = None, *,
                risk_evidence_available: bool = True,
                risk_coverage_by_chain: dict | None = None) -> list[TechChainSignal]:
        sent_map: dict[str, SentimentRow] = {}
        sent_degraded = isinstance(sentiment, Passthrough)
        if not sent_degraded:
            sent_map = sentiment
        alert_list: list[RiskAlert] = []
        risk_degraded = isinstance(alerts, Passthrough) or not risk_evidence_available
        if not risk_degraded or risk_coverage_by_chain is not None and not isinstance(alerts, Passthrough):
            alert_list = alerts
        if risk_coverage_by_chain is not None:
            if not isinstance(risk_coverage_by_chain, dict) or set(risk_coverage_by_chain) - set(TECH_SUBCHAINS):
                raise ValueError("Invalid per-chain risk coverage")
            if any(alert.chain_id not in TECH_SUBCHAINS for alert in alert_list):
                raise ValueError("V2 risk alerts must identify their own target")

        mon_map = {r.chain_id: r for r in monitor}
        signals: list[TechChainSignal] = []
        for cid, spec in TECH_SUBCHAINS.items():
            row = mon_map.get(cid)
            parts: dict[str, tuple[float, float]] = {}  # 维度 -> (得分, 权重)
            degraded: list[str] = []
            evidence: list[str] = []

            s_mom = _score_momentum(row) if row else None
            if s_mom is not None:
                parts["mom"] = (s_mom, W_MOM)
                evidence.append(f"链20日动量 {row.chain_mom20:+.1%} 领涨环节 {row.leading_link}")
            else:
                degraded.append("tech.monitor")

            s_glob = _score_global(cid, linkage)
            if s_glob is not None:
                parts["global"] = (s_glob, W_GLOBAL)
                leaders = ",".join(spec["global_leaders"])
                evidence.append(f"全球龙头({leaders})相对强度映射 {s_glob:.1f}/10")
            else:
                degraded.append("tech.cycle_linkage")

            s_sent = _score_sentiment(sent_map.get(cid))
            if s_sent is not None:
                parts["sent"] = (s_sent, W_SENT)
                sr = sent_map[cid]
                evidence.append(f"舆情 {sr.sentiment_score:+.2f} 热度 {sr.heat:.0f} "
                                f"({sr.narrative_change})")
            else:
                degraded.append("tech.sentiment")

            known_alert_level = None
            risk_status, risk_refs = "insufficient_evidence", []
            if risk_coverage_by_chain is None:
                risk_level = 0.0 if risk_evidence_available else None
                if risk_degraded:
                    degraded.append("tech.risk")
                else:
                    s_risk, risk_level = _risk_penalty(cid, alert_list)
                    parts["risk"] = (s_risk, W_RISK)
                    if risk_level > 0:
                        evidence.append(f"风险预警最高等级 {risk_level:.0f}/10")
            else:
                coverage = risk_coverage_by_chain.get(cid, {})
                if not isinstance(coverage, dict):
                    raise ValueError("Invalid chain risk coverage cell")
                risk_status = coverage.get("status", "insufficient_evidence")
                if risk_status not in {"available", "assessed", "insufficient_evidence", "conflicted"}:
                    raise ValueError("Invalid chain risk assessment status")
                source_ids = coverage.get("source_ids", [])
                risk_refs = coverage.get("assessment_source_ids", [])
                if (not isinstance(source_ids, list) or not isinstance(risk_refs, list)
                        or len(source_ids) != len(set(source_ids))
                        or not set(risk_refs) <= set(source_ids)):
                    raise ValueError("Invalid chain risk evidence references")
                mine = [alert for alert in alert_list if alert.chain_id == cid]
                for alert in mine:
                    if (type(alert.severity) not in {int, float} or not math.isfinite(alert.severity)
                            or not 1 <= alert.severity <= 10 or not alert.evidence_ids
                            or not set(alert.evidence_ids) <= set(source_ids)):
                        raise ValueError("Known risk alert lacks its own qualified evidence")
                known_alert_level = max((alert.severity for alert in mine), default=None)
                declared_known = coverage.get("known_alert_level")
                if declared_known is not None and declared_known != (known_alert_level or 0):
                    raise ValueError("Known risk level differs from evidenced alerts")
                risk_level = None
                if risk_status == "assessed" and not risk_degraded:
                    risk_level = coverage.get("risk_level")
                    if (not risk_refs or type(risk_level) not in {int, float}
                            or not math.isfinite(risk_level)
                            or risk_level != (known_alert_level or 0)):
                        raise ValueError("Complete chain assessment differs from known alerts")
                    known_alert_level = known_alert_level or 0
                    parts["risk"] = (10.0 - risk_level, W_RISK)
                else:
                    if coverage.get("risk_level") is not None and risk_status != "assessed":
                        raise ValueError("Incomplete chain risk score must be null")
                    degraded.append("tech.risk")
                if known_alert_level is not None and known_alert_level > 0:
                    evidence.append(f"已核风险预警最高等级 {known_alert_level:.0f}/10"
                                    + ("；整体风险资料仍不足" if risk_level is None else ""))

            if parts:
                wsum = sum(w for _, w in parts.values())
                prosperity = round(sum(s * w for s, w in parts.values()) / wsum, 2)
            else:
                prosperity = None

            # 加成建议：景气映射 0.90..1.15；风险≥8 强制压制到 ≤1.00
            if prosperity is None:
                bonus = 1.0
            else:
                bonus = 0.90 + (prosperity / 10.0) * 0.25
            if risk_level is not None and risk_level >= 8.0:
                bonus = min(bonus, 1.00)
            if known_alert_level is not None and known_alert_level >= 8.0:
                bonus = min(bonus, 1.00)

            signal_class = StructuredTechChainSignal if risk_coverage_by_chain is not None else TechChainSignal
            extra = ({"known_alert_level": known_alert_level, "risk_assessment_status": risk_status,
                      "risk_evidence_ids": list(risk_refs)} if risk_coverage_by_chain is not None else {})
            signals.append(signal_class(
                chain_id=cid,
                main_chain=SUBCHAIN_TO_MAIN.get(cid, "semis"),
                prosperity=prosperity,
                risk_level=risk_level,
                alerts=[{"severity": a.severity, "type": a.type,
                         "headline_zh": a.headline_zh, "link": a.link,
                         "transmission": a.transmission,
                         **({"evidence_ids": list(a.evidence_ids)} if risk_coverage_by_chain is not None else {})}
                        for a in alert_list if a.chain_id in (cid, "unknown")],
                transmission=_transmission_graph(row) if row else {},
                bonus_hint=round(bonus, 3),
                leading_link=row.leading_link if row else None,
                degraded_components=degraded,
                evidence=evidence,
                **extra,
            ))
        return signals


def signals_to_bonus_map(signals: list[TechChainSignal]) -> dict[str, float]:
    """主引擎接入点：主链 id（semis/ai_compute）→ 乘性加成（取子链最强）。"""
    out: dict[str, float] = {}
    for s in signals:
        cur = out.get(s.main_chain, 1.0)
        # 同一主链下多条子链：取偏离 1.0 最远者（最强信号优先）
        out[s.main_chain] = (s.bonus_hint
                             if abs(s.bonus_hint - 1.0) > abs(cur - 1.0) else cur)
    # A severe v2 subchain warning cannot be overwritten by a different strong
    # subchain when both are folded into the same main-chain bonus.
    for signal in signals:
        if (isinstance(signal, StructuredTechChainSignal) and signal.known_alert_level is not None
                and signal.known_alert_level >= 8):
            out[signal.main_chain] = min(out[signal.main_chain], 1.0)
    return out
