"""DebateAgent — 多空辩论证据层（桥水 AIA 辩论制，v6.3 S2）。

三个角色：
  Bull        多头论据（为什么该买）
  Bear        空头 / 魔鬼代言人论据（为什么不该买 / 哪里会错）
  Coordinator 裁决 + 结构化落盘（防回音室：只传结论与证据，不做二次创作）

铁律（白皮书 D9/D6 的机器执行）：
  - 辩论【绝不修改】任何分数与闸门输出（MRS/SHS/ICS/TSS/TOS/五态行动一律不变），
    产出只作为交易卡片"第六段：多空辩论证据"附加进报告；
  - 触发范围收敛到【灰区】（轻仓通道候选 / HOLD 区高分标的），标准 BUY 与
    AVOID 日不辩论，每日上限 config.DEBATE_MAX_PER_DAY（防算力失控）；
  - 本环节是 LLM 环节（decision.debate）：LLM 不可用的唯一出口是
    llm_guard 透传兜底，交易卡片第六段标注"本轮无辩论证据"，
    代码库中不存在"LLM 失败 → 规则生成辩论内容"的分支；
  - 回测路径【禁用】：backtest 不经过 pipeline，本 Agent 无从被调用
    （无未来函数：历史回放禁止注入 LLM 语义）。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from .. import config
from ..llm.client import LLMClient
from ..llm.prompts import DEBATE_SCHEMA, DEBATE_USER_TMPL, SYSTEM_ANALYST
from ..redline import ExecutionTracer, Passthrough, llm_guard
from ..search.models import CleanDocument

log = logging.getLogger("agents.debate")

VERDICTS = ("证据充分", "存疑", "反对")


@dataclass
class DebateEvidence:
    """一只标的的多空辩论证据（只读证据，不含任何分数/闸门字段）。"""
    symbol: str
    bull_points: list[str] = field(default_factory=list)
    bear_points: list[str] = field(default_factory=list)
    coordinator_verdict: str = "存疑"        # 证据充分 / 存疑 / 反对
    verdict_reason: str = ""
    falsification_conditions: list[str] = field(default_factory=list)
    llm_used: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def select_debate_targets(picks: list, rationale: dict, watchlist: list,
                          action: str, max_per_day: int | None = None) -> list[dict]:
    """纯规则触发判定（确定性：同输入同输出，灰区才辩论）。

    ① 轻仓通道候选：rationale.standard=False（MRS*∈[5.5,6.0) 或仅次主线
       且 TSS_final≥轻仓门槛——正是闸门判定最"纠结"的一批）；
    ② HOLD 区高分标的：action=="HOLD" 时 watchlist 中
       TSS_final ≥ DEBATE_HOLD_TSS_MIN 的候选（高分但未放行，值得魔鬼代言人复核）；
    标准 BUY（rationale.standard=True）与 AVOID/WAIT 日不辩论。
    返回按优先级排序、截断至 max_per_day 的辩论输入包列表。
    """
    cap = config.DEBATE_MAX_PER_DAY if max_per_day is None else max_per_day
    if cap <= 0:
        return []
    targets: list[dict] = []
    seen: set[str] = set()

    # ① 轻仓通道候选（按 TOS 排序，与放行顺序一致）
    for p in picks:
        info = rationale.get(p.ticker) or {}
        if info.get("standard"):
            continue                                # 标准 BUY 不辩论
        targets.append({
            "symbol": p.ticker, "tss_final": p.tss_final,
            "mode": info.get("mode", "轻仓试错"),
            "template": p.entry_template or "",
            "chain": p.chain or "", "sector": p.sector or "",
            "shs": info.get("shs"), "mrs_star": info.get("mrs_star"),
            "draft": (f"轻仓通道放行：TSS_final={p.tss_final}，"
                      f"SHS={info.get('shs')}，MRS*={info.get('mrs_star')}，"
                      f"止损 {p.stop_price}，时间纪律 {p.time_stop_days} 日"),
        })
        seen.add(p.ticker)

    # ② HOLD 区高分标的（高分但未过闸，补充魔鬼代言人视角）
    if action == "HOLD":
        for c in sorted(watchlist, key=lambda x: x.tss_final, reverse=True):
            if c.ticker in seen or c.tss_final < config.DEBATE_HOLD_TSS_MIN:
                continue
            targets.append({
                "symbol": c.ticker, "tss_final": c.tss_final,
                "mode": "HOLD区高分未放行",
                "template": c.entry_template or "",
                "chain": c.chain_id or "", "sector": c.sector_etf or "",
                "shs": None, "mrs_star": None,
                "draft": (f"HOLD 区高分候选：TSS_final={c.tss_final}，"
                          f"模板 {c.entry_template or '无'}，证伪草稿: {c.stop_plan}"),
            })
            seen.add(c.ticker)

    return targets[:cap]


class DebateAgent:
    """多空辩论 LLM 环节（红线 2：只有 LLM 一条产出路径）。"""

    name = "Debate-Agent"
    layer = 4

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def execute(self, targets: list[dict], docs: list[CleanDocument],
                tracer: ExecutionTracer | None = None
                ) -> dict[str, DebateEvidence] | Passthrough:
        """对触发标的逐只辩论。返回 {symbol: DebateEvidence}；
        LLM 不可用/全部无有效产出 → Passthrough（报告标注"本轮无辩论证据"）。"""
        def _run() -> dict[str, DebateEvidence] | None:
            out: dict[str, DebateEvidence] = {}
            for t in targets:
                ev = self._debate_one(t, docs)
                if ev is not None:
                    out[t["symbol"]] = ev
            return out or None

        return llm_guard("decision.debate", _run, fallback_payload=targets,
                         tracer=tracer)

    # ------------------------------------------------------------

    def _debate_one(self, target: dict, docs: list[CleanDocument]
                    ) -> DebateEvidence | None:
        """单只标的一轮辩论：Bull/Bear 独立陈述，Coordinator 只据双方
        结论与证据裁决（防回音室）。证伪条件为空 = 无效产出（纪律约束）。"""
        symbol = target["symbol"]
        payload = self._doc_payload(symbol, docs)
        out = self.llm.complete_json(
            system=SYSTEM_ANALYST,
            user=DEBATE_USER_TMPL.format(
                symbol=symbol, mode=target.get("mode", ""),
                tss=target.get("tss_final"), template=target.get("template") or "无",
                chain=target.get("chain") or "无", sector=target.get("sector") or "无",
                draft=target.get("draft", ""), docs=payload),
            schema_hint=DEBATE_SCHEMA)
        bull = [str(x) for x in (out.get("bull_points") or []) if str(x).strip()][:5]
        bear = [str(x) for x in (out.get("bear_points") or []) if str(x).strip()][:5]
        fals = [str(x) for x in (out.get("falsification_conditions") or [])
                if str(x).strip()][:5]
        if not fals:
            # 铁律：无证伪条件的辩论不是辩论（不可证伪 = 不可交易），丢弃该产出
            log.warning("debate(%s): LLM 产出缺证伪条件，丢弃该标的辩论证据", symbol)
            return None
        verdict = str(out.get("coordinator_verdict") or "").strip()
        if verdict not in VERDICTS:
            verdict = "存疑"
        return DebateEvidence(
            symbol=symbol, bull_points=bull, bear_points=bear,
            coordinator_verdict=verdict,
            verdict_reason=str(out.get("verdict_reason") or "")[:300],
            falsification_conditions=fals, llm_used=True)

    @staticmethod
    def _doc_payload(symbol: str, docs: list[CleanDocument]) -> str:
        """优先取提及该标的的清洗后文档（本轮真实证据），不足时补前排文档。"""
        rel = [c for c in docs if symbol in (c.tickers or [])]
        pool = (rel + [c for c in docs if c not in rel])[:10]
        return "\n\n".join(
            f"<doc id=\"{c.raw.doc_id}\" source=\"{c.raw.source}\">"
            f"{c.raw.content[:600]}</doc>" for c in pool) or "（本轮无相关文档证据）"
