"""TigerTest intelligence-first focus policy.

The focus profile narrows the market universe to one reviewed domain with two
executable parent themes:

* ``semiconductor_cycle`` -> the reviewed ``semis`` chain
* ``ai_infrastructure`` -> the reviewed ``ai_compute`` chain

This module is deterministic. It shapes the researched universe, research
questions and decision surface, but it never relaxes MRS/SHS/ICS/TSS or final
risk gates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable

from . import config
from .chains import CHAINS, chain_of
from .tech_chain.universe import SUBCHAIN_TO_MAIN, TECH_SUBCHAINS
from .universe import RETIRED_UNIVERSE

TECH_FOCUS_CONTRACT = "tiger.tech-focus.v1"
TECH_FOCUS_VERSION = "1.0.0"

THEMES: tuple[dict, ...] = (
    {
        "id": "semiconductor_cycle",
        "name_zh": "半导体周期",
        "parent_chains": ["semis"],
        "subchains": ["memory", "logic", "foundry", "equipment"],
        "sector_etfs": ["SMH", "XLK"],
    },
    {
        "id": "ai_infrastructure",
        "name_zh": "AI 基础设施",
        "parent_chains": ["ai_compute"],
        "subchains": ["ai_model", "ai_app"],
        "sector_etfs": ["XLK", "SMH"],
    },
)


def _digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TechFocusPolicy:
    contract: str = TECH_FOCUS_CONTRACT
    version: str = TECH_FOCUS_VERSION
    market: str = "us"
    universe_mode: str = config.TECH_FOCUS_UNIVERSE_MODE
    themes: tuple[dict, ...] = THEMES
    sector_etfs: tuple[str, ...] = config.TECH_FOCUS_SECTOR_ETFS
    parent_chains: tuple[str, ...] = config.TECH_FOCUS_PARENT_CHAINS
    min_price: float = config.TECH_FOCUS_MIN_PRICE
    min_adv_usd: int = config.TECH_FOCUS_MIN_ADV_USD
    min_history_days: int = config.TECH_FOCUS_MIN_HISTORY_DAYS
    max_atr_pct: float = config.TECH_FOCUS_MAX_ATR_PCT
    frontier_size: int = config.TECH_FOCUS_FRONTIER_SIZE
    theme_minimum: int = config.TECH_FOCUS_THEME_MINIMUM
    max_picks: int = config.TECH_FOCUS_MAX_PICKS
    max_conditional_watch: int = config.TECH_FOCUS_MAX_CONDITIONAL_WATCH

    def to_dict(self) -> dict:
        return {
            "contract": self.contract,
            "version": self.version,
            "market": self.market,
            "universe_mode": self.universe_mode,
            "themes": [dict(theme) for theme in self.themes],
            "sector_etfs": list(self.sector_etfs),
            "parent_chains": list(self.parent_chains),
            "min_price": self.min_price,
            "min_adv_usd": self.min_adv_usd,
            "min_history_days": self.min_history_days,
            "max_atr_pct": self.max_atr_pct,
            "frontier_size": self.frontier_size,
            "theme_minimum": self.theme_minimum,
            "max_picks": self.max_picks,
            "max_conditional_watch": self.max_conditional_watch,
        }

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())


FOCUS_POLICY = TechFocusPolicy()


def focus_universe() -> list[str]:
    """Return the reviewed US-listed members of the two focus parent chains."""
    out: list[str] = []
    seen: set[str] = set()
    for chain_id in FOCUS_POLICY.parent_chains:
        chain = CHAINS[chain_id]
        for link in ("upstream", "midstream", "downstream"):
            for ticker in chain[link]["tickers"]:
                # Parent chains include no reviewed foreign ordinary listings,
                # but keep the guard explicit so future edits cannot widen the
                # US cash-equity workflow silently.
                if "." in ticker or ticker in RETIRED_UNIVERSE or ticker in seen:
                    continue
                seen.add(ticker)
                out.append(ticker)
    return out


TECH_FOCUS_UNIVERSE = tuple(focus_universe())


def scan_filters() -> dict:
    return {
        "min_price": FOCUS_POLICY.min_price,
        "min_adv": FOCUS_POLICY.min_adv_usd,
        "min_history": FOCUS_POLICY.min_history_days,
        "max_atr_pct": FOCUS_POLICY.max_atr_pct,
        "currency": "USD",
    }


def themes_for_chain(chain_id: str | None) -> list[str]:
    """Resolve one reviewed parent/subchain to its executable theme label."""
    if not chain_id:
        return []
    parent = SUBCHAIN_TO_MAIN.get(chain_id, chain_id)
    return [
        theme["id"] for theme in FOCUS_POLICY.themes
        if parent in theme["parent_chains"] or chain_id in theme["subchains"]
    ]


def themes_for_symbol(symbol: str) -> list[str]:
    return themes_for_chain(chain_of(symbol)[0])


def select_frontier_rows(ranked: list[dict], *, limit: int | None = None,
                         theme_minimum: int | None = None) -> list[dict]:
    """Select a deep-research frontier with a minimum slot for each focus theme.

    The global rank order remains authoritative. Theme slots only guarantee
    that one parent theme cannot starve the other when their market momentum
    diverges.
    """
    limit = FOCUS_POLICY.frontier_size if limit is None else limit
    theme_minimum = FOCUS_POLICY.theme_minimum if theme_minimum is None else theme_minimum
    if type(limit) is not int or limit <= 0:
        raise ValueError("Focus frontier limit must be a positive integer")
    if type(theme_minimum) is not int or theme_minimum < 0:
        raise ValueError("Focus theme minimum must be a nonnegative integer")

    eligible = [
        dict(row) for row in ranked
        if isinstance(row, dict) and isinstance(row.get("ticker"), str)
        and row["ticker"] in TECH_FOCUS_UNIVERSE
    ]
    buckets: dict[str, list[dict]] = {theme["id"]: [] for theme in FOCUS_POLICY.themes}
    for row in eligible:
        for theme_id in themes_for_symbol(row["ticker"]):
            buckets[theme_id].append(row)

    selected: set[str] = set()
    for theme in FOCUS_POLICY.themes:
        for row in buckets[theme["id"]][:theme_minimum]:
            selected.add(row["ticker"])
    for row in eligible:
        if len(selected) >= limit:
            break
        selected.add(row["ticker"])
    return [row for row in eligible if row["ticker"] in selected][:limit]


def frontier_selection_reason(symbol: str, rank_index: int, limit: int) -> str:
    themes = themes_for_symbol(symbol)
    prefix = "focus_theme_quota" if rank_index >= limit else "focus_rank"
    return prefix + (":" + ",".join(themes) if themes else ":unmapped")


def _cell(target_id: str, question: str, query: str, *,
          material_type: str = "news", window_days: int = 30,
          entity_id: str | None = None, required: bool = True) -> dict:
    value = {
        "target_id": target_id,
        "question": question,
        "query": query,
        "required": required,
        "material_type": material_type,
        "window_days": window_days,
        "entity_id": entity_id,
        "topic": target_id.split(":", 1)[-1],
    }
    return {"cell_id": "cell_" + _digest(value)[:24], **value}


_THEME_QUESTIONS: tuple[tuple[str, str, str, str, int], ...] = (
    (
        "global:macro",
        "利率、通胀和风险偏好是否支持久期型科技资产",
        "Federal Reserve FOMC CPI PCE Treasury yields technology equity risk appetite",
        "macro_release",
        30,
    ),
    (
        "chain:memory",
        "存储价格、库存和上游资本开支处于什么周期",
        "DRAM NAND memory pricing inventory Samsung SK Hynix Micron capex",
        "news",
        30,
    ),
    (
        "chain:memory",
        "存储链是否存在供应、出口管制或需求反转反证",
        "memory export controls supply disruption demand correction Samsung SK Hynix Micron",
        "adverse_event",
        90,
    ),
    (
        "chain:logic",
        "GPU、CPU 和定制 ASIC 的设计需求是否继续上修",
        "GPU CPU custom ASIC AI accelerator demand Nvidia AMD Broadcom Marvell",
        "news",
        30,
    ),
    (
        "chain:logic",
        "逻辑芯片链的竞争、估值和替代架构风险",
        "AI chip competition valuation custom silicon substitution Nvidia AMD Broadcom",
        "counterevidence",
        90,
    ),
    (
        "chain:foundry",
        "晶圆代工利用率、先进封装和产能扩张是否验证景气",
        "TSMC utilization CoWoS advanced packaging wafer capacity capex",
        "news",
        30,
    ),
    (
        "chain:foundry",
        "代工链是否出现利用率下降、砍单或产能过剩反证",
        "foundry utilization cut orders overcapacity TSMC Samsung Intel",
        "adverse_event",
        90,
    ),
    (
        "chain:equipment",
        "半导体设备订单和 book-to-bill 是否领先改善",
        "semiconductor equipment orders book to bill ASML Applied Materials Lam KLA",
        "news",
        30,
    ),
    (
        "chain:equipment",
        "设备链的出口限制、中国收入和订单延期风险",
        "semiconductor equipment export controls China revenue order delay",
        "adverse_event",
        90,
    ),
    (
        "chain:ai_model",
        "hyperscaler AI 资本开支、云收入和订单可见度是否兑现",
        "hyperscaler AI capex Microsoft Google Amazon Meta Oracle data center demand",
        "news",
        30,
    ),
    (
        "chain:ai_model",
        "AI 基础设施的电力、供给瓶颈、折旧和回报率风险",
        "AI data center power constraint depreciation capex return risk Microsoft Google Meta",
        "counterevidence",
        90,
    ),
    (
        "chain:ai_app",
        "企业 AI 软件采用、ARR、续费率与变现是否改善",
        "enterprise AI software ARR adoption renewal monetization Palantir Salesforce ServiceNow",
        "news",
        30,
    ),
    (
        "chain:ai_app",
        "AI 应用链的定价压力、席位增长放缓与概念证伪",
        "AI software pricing pressure seat growth slowdown monetization counterevidence",
        "counterevidence",
        90,
    ),
    (
        "sector:XLK",
        "科技板块资金动量、集中度和叙事是否仍支持主线",
        "XLK technology sector fund flows concentration earnings guidance",
        "news",
        30,
    ),
    (
        "sector:SMH",
        "半导体板块广度、周期拐点和拥挤风险",
        "SMH semiconductor breadth cycle turning point crowding risk",
        "news",
        30,
    ),
)


def research_cells(frontier: dict) -> list[dict]:
    """Build the bounded, deeper two-theme research graph for one frontier."""
    if not isinstance(frontier, dict) or not isinstance(frontier.get("symbols"), list):
        raise ValueError("A frozen focus frontier is required")
    if len(frontier["symbols"]) > FOCUS_POLICY.frontier_size:
        raise ValueError("Focus frontier exceeds its reviewed size")

    cells = [
        _cell(target, question, query, material_type=material, window_days=days)
        for target, question, query, material, days in _THEME_QUESTIONS
    ]
    security_ids = frontier.get("security_ids", {})
    for symbol in frontier["symbols"]:
        security_id = security_ids.get(symbol)
        if not isinstance(security_id, str):
            raise ValueError("Frozen frontier lacks a reviewed security identity")
        target = "security:" + security_id
        cells.extend([
            _cell(
                target,
                "最新财报基线与管理层指引",
                f"{symbol} earnings annual quarterly report guidance outlook",
                material_type="issuer_financial",
                window_days=760,
                entity_id=security_id,
            ),
            _cell(
                target,
                "当前重大不利事项与监管事件",
                f"{symbol} recall lawsuit investigation warning material event",
                material_type="adverse_event",
                window_days=45,
                entity_id=security_id,
            ),
            _cell(
                target,
                "反证、更正、撤销与竞争替代",
                f"{symbol} correction restatement withdrawal contrary evidence risk",
                material_type="counterevidence",
                window_days=90,
                entity_id=security_id,
            ),
        ])
    return cells


def research_topics() -> dict[str, str]:
    """Legacy SearchHub-compatible topic set used only when full v2 is absent."""
    return {
        "memory": TECH_SUBCHAINS["memory"]["name"] + " DRAM NAND inventory pricing capex",
        "logic": TECH_SUBCHAINS["logic"]["name"] + " GPU CPU accelerator demand",
        "foundry": TECH_SUBCHAINS["foundry"]["name"] + " utilization CoWoS packaging",
        "equipment": TECH_SUBCHAINS["equipment"]["name"] + " book-to-bill export controls",
        "ai_model": TECH_SUBCHAINS["ai_model"]["name"] + " hyperscaler capex cloud returns",
        "ai_app": TECH_SUBCHAINS["ai_app"]["name"] + " ARR monetization enterprise adoption",
        "XLK": "XLK technology sector flows breadth earnings guidance",
        "SMH": "SMH semiconductor sector breadth cycle crowding",
    }


def _finite(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def build_theme_dossier(context: dict) -> dict:
    """Project numeric chain facts and evidence states into the two parent themes."""
    chain_map = context.get("chain_map") or {}
    signal_map = {
        row["chain_id"]: row for row in (context.get("tech_signals") or [])
        if isinstance(row, dict) and isinstance(row.get("chain_id"), str)
    }
    disclosure = context.get("focus_research_status") or {}
    gaps = disclosure.get("gaps") or []
    themes = []
    for theme in FOCUS_POLICY.themes:
        subchains = []
        for chain_id in theme["subchains"]:
            parent_chain = SUBCHAIN_TO_MAIN.get(chain_id)
            chain = chain_map.get(parent_chain)
            signal = signal_map.get(chain_id)
            if hasattr(signal, "to_dict"):
                signal = signal.to_dict()
            signal = signal if isinstance(signal, dict) else {}
            subchains.append({
                "chain_id": chain_id,
                "name_zh": chain.name if chain is not None else TECH_SUBCHAINS[chain_id]["name"],
                "ics": chain.ics if chain is not None and _finite(chain.ics) else None,
                "stage": chain.stage if chain is not None else "unavailable",
                "hot": bool(chain.hot) if chain is not None else False,
                "breadth_pct": (round(chain.breadth, 2) if chain is not None
                                and _finite(chain.breadth) else None),
                "prosperity": (signal.get("prosperity")
                               if _finite(signal.get("prosperity")) else None),
                "known_alert_level": (signal.get("known_alert_level")
                                      if _finite(signal.get("known_alert_level")) else None),
                "risk_assessment_status": signal.get("risk_assessment_status", "unavailable"),
                "degraded_components": list(signal.get("degraded_components") or []),
            })
        theme_gaps = [
            gap for gap in gaps
            if gap.get("target_id", "").split(":", 1)[-1] in theme["subchains"]
            or gap.get("target_id") in {"sector:" + etf for etf in theme["sector_etfs"]}
        ]
        themes.append({
            **dict(theme),
            "subchain_states": subchains,
            "research_gap_count": len(theme_gaps),
        })
    return {
        "contract": TECH_FOCUS_CONTRACT,
        "policy_sha256": FOCUS_POLICY.sha256,
        "themes": themes,
        "evidence_state": disclosure.get("state", "unknown"),
    }


_REASON_TRIGGERS = {
    "mrs_below_standard": "MRS* 回到 6.0 以上",
    "market_block": "MRS* 回到 4.0 以上并重新取得新开仓许可",
    "kill_switch": "Kill Switch 解除且市场冲击条件重新验证",
    "sector_outside_standard_channel": "所属板块进入主线池（SHS≥7.5、广度≥60%）或链热区且 SHS≥7.0",
    "no_legal_probe_channel": "进入 MRS* 5.5–6.0 轻仓区，或所属板块进入次主线",
    "tss_below_standard": "TSS_final 回到 7.2 以上",
    "tss_below_probe": "TSS_final 回到 7.8 以上且满足轻仓通道",
    "entry_template_missing": "等待 A/B/C 可执行入场模板",
    "sector_evidence_missing": "补齐已审定的板块/产业链映射",
    "sector_chase_evidence_missing": "补齐板块广度与关键位证据",
    "sector_breadth_no_chase": "价格回到关键位及以下，且不再处于低广度追高",
    "invalid_entry_or_stop": "重新取得有效入场价与低于入场价的结构止损",
    "invalid_stop_distance": "取得可执行的正向结构止损距离",
    "risk_budget_below_one_share": "释放风险预算或出现更近的结构止损",
    "position_budget_below_one_share": "释放单票市值预算",
    "position_slots_exhausted": "等待现有持仓释放位置名额",
    "group_risk_budget_exhausted": "释放同产业链风险预算或降低相关性暴露",
    "gross_budget_exhausted": "释放总仓位预算",
    "existing_or_pending_position": "现有持仓或待成交记录退出后重新评估",
    "existing_budget_unverifiable": "修复现有组合预算回执",
    "compliance_rejected": "满足美股合规围栏并重新验证",
    "currency_blocked": "恢复 USD 计价与记账契约",
    "research_missing": "先完成该证券的冻结原文与逐目标证据研究",
    "outside_frozen_research_frontier": "发起新的补充研究，不能沿用本轮交易许可",
}


def _candidate_reasons(row: dict | None, *, model_decision: dict | None = None) -> list[str]:
    reasons = list((row or {}).get("reasons") or [])
    if model_decision is not None and model_decision.get("action") != "buy":
        reasons.append("model_veto")
    return list(dict.fromkeys(reasons))


def _triggers(reasons: Iterable[str], *, candidate=None) -> list[str]:
    triggers = []
    for reason in reasons:
        if reason == "model_veto":
            triggers.append("模型终审转为买入，且公司证据与行情证据仍保持一致")
        elif reason in _REASON_TRIGGERS:
            triggers.append(_REASON_TRIGGERS[reason])
        else:
            triggers.append("修复闸门原因后重新完成数值评分与证据核验：" + str(reason))
    if candidate is not None and getattr(candidate, "key_level", 0) > 0:
        triggers.append(f"观察关键位 {candidate.key_level:.2f}，只接受白皮书 A/B/C 模板触发")
    return list(dict.fromkeys(triggers)) or ["等待下一轮完整数据、证据和闸门检查"]


def build_decision_surface(context: dict, *, final: bool = False) -> dict:
    """Always return an actionable decision surface, including conditional names.

    A WAIT day is not an empty day. For every near candidate, this projection
    states whether it is blocked by market, theme, setup, evidence or budget,
    and what observable condition would reopen evaluation.
    """
    candidates = list(context.get("watchlist") or [])
    picks = {pick.ticker: pick for pick in (context.get("picks") or [])}
    rejection = context.get("rejections") or {}
    diagnostics = {
        row.get("ticker"): row for row in (rejection.get("candidates") or [])
        if isinstance(row, dict) and isinstance(row.get("ticker"), str)
    }
    model_decisions = {}
    debate = context.get("debate") or {}
    model_output = debate.get("model_output") if isinstance(debate, dict) else None
    if isinstance(model_output, dict):
        model_decisions = {
            row["symbol"]: row for row in model_output.get("decisions", [])
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }

    actionable = []
    for symbol, pick in picks.items():
        actionable.append({
            "ticker": symbol,
            "state": "ACTIONABLE",
            "tss_final": pick.tss_final,
            "entry_template": pick.entry_template,
            "entry_price": pick.entry_price,
            "stop_price": pick.stop_price,
            "shares": pick.shares,
            "position_pct": pick.position_pct,
            "risk_usd": pick.risk_usd,
            "why_now": (context.get("pick_rationale", {}).get(symbol) or {}),
        })

    watch = []
    for candidate in sorted(candidates, key=lambda item: item.tss_final, reverse=True):
        if candidate.ticker in picks:
            continue
        model_decision = model_decisions.get(candidate.ticker) if final else None
        reasons = _candidate_reasons(diagnostics.get(candidate.ticker), model_decision=model_decision)
        if candidate.entry_template not in {"A", "B", "C"}:
            reasons.append("entry_template_missing")
        reasons = list(dict.fromkeys(reasons))
        state = ("CONDITIONAL" if candidate.entry_template in {"A", "B", "C"}
                 else "SETUP_PENDING")
        watch.append({
            "ticker": candidate.ticker,
            "state": state,
            "themes": themes_for_symbol(candidate.ticker),
            "chain_id": candidate.chain_id,
            "sector_etf": candidate.sector_etf,
            "rank_score": candidate.rank_score,
            "tss_final": candidate.tss_final,
            "entry_template": candidate.entry_template or None,
            "price": candidate.price,
            "key_level": candidate.key_level if candidate.key_level > 0 else None,
            "stop_price": candidate.stop_price if candidate.stop_price > 0 else None,
            "reasons": reasons,
            "reopen_if": _triggers(reasons, candidate=candidate),
            "research_note": (model_decision or {}).get("action_summary"),
        })
        if len(watch) >= FOCUS_POLICY.max_conditional_watch:
            break

    # Hard-filter rejection or an all-empty scan can still leave a useful
    # preliminary list from the frozen market-only ranking.
    if not candidates:
        frontier = context.get("focus_frontier") or {}
        for row in (frontier.get("ranked") or [])[:5]:
            watch.append({
                "ticker": row.get("ticker"),
                "state": "PRELIMINARY",
                "themes": themes_for_symbol(row.get("ticker", "")),
                "rank_score": row.get("rank"),
                "price": row.get("price"),
                "reasons": ["no_live_focus_candidate"],
                "reopen_if": ["重新取得完整 260 日行情并通过流动性/波动率硬过滤"],
            })

    disclosure = context.get("focus_research_status") or {}
    research_gaps = []
    for gap in (disclosure.get("gaps") or [])[:20]:
        research_gaps.append({
            "target": gap.get("target_label") or gap.get("target_id"),
            "question": gap.get("question"),
            "status": gap.get("status"),
            "reason": gap.get("reason"),
        })

    next_open = [
        f"MRS* ≥ {config.OPEN_LONG['mrs']:.1f}（轻仓区 ≥ {config.LIGHT_PROBE['mrs_lo']:.1f}）",
        "XLK 或 SMH 进入主线池；主线入池要求 SHS≥7.5 且广度≥60%",
        f"目标处于热区产业链并取得 TSS_final≥{config.OPEN_LONG['tss']:.1f} 或轻仓门槛≥{config.LIGHT_PROBE['tss']:.1f}",
        "出现 A/B/C 可执行入场模板、结构止损和正向风险预算",
        "公司证据、反证与宏观证据均完成逐目标覆盖；缺口不能用“没有坏消息”代替",
    ]
    return {
        "contract": "tiger.decision-surface.v1",
        "focus_policy_sha256": FOCUS_POLICY.sha256,
        "final": final,
        "action": context.get("action", "WAIT"),
        "market_view": context.get("market_view", ""),
        "themes": build_theme_dossier(context)["themes"],
        "actionable": actionable,
        "conditional_watchlist": watch,
        "research_gaps": research_gaps,
        "next_open_conditions": next_open,
        "rejection_summary": {
            key: rejection.get(key) for key in
            ("candidate_count", "status_counts", "overlapping_reason_counts")
        },
        "not_a_trade_approval": True,
    }
