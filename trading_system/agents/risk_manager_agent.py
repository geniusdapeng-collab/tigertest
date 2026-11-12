"""Risk Manager Agent（Layer 4）— 闸门 / 仓位反推 / 交易卡片。

执行理论硬规则（§5 三分数联动 + §6 R 风控 + §7 事件折扣）：
  标准做多：MRS*≥6.0 且主线 SHS≥7.5 且 TSS_final≥7.2
  轻仓试错：MRS*∈[5.5,6) 或 SHS∈[7.0,7.5)，但 TSS_final≥7.8，仓位×0.3-0.4
  禁止项：  MRS*<4.0 不开新仓；主线广度<40% 不追高
  仓位反推：Shares = Account×r / |P_in − P_sl|（r=0.8% 默认）
  TOS       = MRS* × SHS(所属主线) × TSS_final × C_liq（0-10 归一化排序用）
"""

from __future__ import annotations

import math

from .. import config
from ..chains import CHAINS, chain_name_zh
from ..currency import usd_execution_policy
from ..data_models import (
    ChainState, MRSResult, SectorScore, StockCandidate, TradePick,
)
from .base import BaseAgent


class RiskManagerAgent(BaseAgent):
    name = "Risk-Manager-Agent"
    layer = 4

    def __init__(self, provider, account_usd: float = 100_000,
                 max_picks: int = config.MAX_PICKS_DEFAULT):
        super().__init__(provider)
        self.account = account_usd
        self.max_picks = max_picks

    def execute(self, context: dict) -> list[TradePick]:
        from ..gate import pass_gates
        from ..activity import (enabled, execution_budgeted, gate_reasons, portfolio_reservations,
                                rejection_report, risk_group, sector_evidence)

        mrs: MRSResult = context["mrs"]
        sectors: list[SectorScore] = context.get("sectors", [])
        candidates: list[StockCandidate] = context.get("watchlist", [])
        chain_map: dict[str, ChainState] = context.get("chain_map", {})
        active = enabled(context)
        budgeted = execution_budgeted(context)
        diagnostics = {c.ticker: {"ticker": c.ticker, "status": "candidate", "reasons": []}
                       for c in candidates}
        existing_gross, existing_groups, existing_tickers, incomplete_budget = (
            portfolio_reservations(context.get("portfolio_state"), self.account)
            if budgeted else (0.0, {}, set(), False))

        main_sectors = [s for s in sectors if s.in_main_pool]
        sub_sectors = [s for s in sectors if s.in_sub_pool]
        notes: list[str] = []

        # ---- S4 多市场：市场规格 / 合规围栏前置 / 轻仓通道毕业门槛（D7）----
        spec = context.get("market_spec")                # MarketSpec | None
        currency_policy = usd_execution_policy(spec, tickers=(c.ticker for c in candidates))
        context["currency_policy"] = currency_policy
        if currency_policy["execution_blocked"]:
            notes.append(currency_policy["reason"])
        grad = context.get("market_grad") or {"graduated": True, "settled": 0,
                                              "required": config.MARKET_GRAD_MIN_SETTLED}
        grad_forced = not grad.get("graduated", True)
        compliance: list[dict] = []                      # 校验记录（日报披露）
        if grad_forced:
            light_size = round(sum(config.MARKET_LIGHT_SIZE) / 2, 4)
            notes.append(
                f"新市场验证期（{grad.get('settled', 0)}/{grad.get('required')}，"
                f"DSR 未过关）：本市场全部放行标的强制轻仓 ×{light_size}"
                f"（D7：满 {grad.get('required')} 笔结算且 DSR 过关才开放标准通道）")

        # ---- 闸门（MRS*<4.0 或 Kill Switch 触发 → 禁新开仓）----
        if not mrs.allow_new_positions:
            for row in diagnostics.values():
                row.update(status="rejected", reasons=["kill_switch" if mrs.shock else "market_block"])
            context["rejections"] = rejection_report(list(diagnostics.values()))
            context["action"] = "AVOID"
            if mrs.shock:
                view = (f"Kill Switch 触发：{mrs.shock_reason} — 停止新开仓，"
                        "优先降总敞口，只允许减仓与对冲（白皮书§10.4）")
                notes.append(view)
            else:
                view = f"MRS*={mrs.mrs_star}（{mrs.regime}）— 禁止新开波段仓，防守/对冲/空仓"
                notes.append("理论§5.3：MRS*<4.0，除对冲白名单外不开新仓")
            context["market_view"] = view
            if currency_policy["execution_blocked"]:
                context["market_view"] += "；" + currency_policy["reason"]
                context["pick_rationale"] = {}
            context["picks"] = []
            context["notes"] = notes
            return []

        # ---- v6.0 事件折扣（白皮书§11/§4.5）：当日 Gross Cap 折扣 ----
        cal = context.get("event_calendar")
        trade_date = context.get("trade_date")
        day_factor, day_notes = (cal.day_discount(trade_date) if cal else (1.0, []))
        notes.extend(day_notes)
        gross_cap = round(mrs.position_cap[1] * day_factor, 4)

        # ---- 主线归属判定：候选股所属板块/产业链是否命中主线池 ----
        def sector_shs(etf: str) -> float:
            for s in sectors:
                if s.etf == etf:
                    return s.shs
            return config.NEUTRAL_SCORE

        picks: list[TradePick] = []
        # 每个放行标的的判定要素全透传（报告层"决策依据"直接消费，禁止反推）
        rationale: dict[str, dict] = {}
        light_probe = config.LIGHT_PROBE
        mrs_light = light_probe["mrs_lo"] <= mrs.mrs_star < config.OPEN_LONG["mrs"]

        for c in candidates:
            # 所属板块 SHS：优先产业链映射 ETF，其次 chain ETF，最后中性
            evidence = sector_evidence(c.ticker, c.chain_id, active=active)
            etf = evidence["etf"]
            shs = sector_shs(etf) if etf else config.NEUTRAL_SCORE
            c.sector_etf = etf
            row = diagnostics[c.ticker]
            row.update(sector_evidence=evidence, mrs_star=mrs.mrs_star,
                       shs=shs, tss_final=c.tss_final)
            if budgeted and c.entry_template not in {"A", "B", "C"}:
                row.update(status="rejected", reasons=["entry_template_missing"])
                continue
            if active and (not etf or not any(s.etf == etf for s in sectors)):
                row.update(status="rejected", reasons=["sector_evidence_missing"])
                continue
            if budgeted and c.ticker in existing_tickers:
                row.update(status="rejected", reasons=["existing_or_pending_position"])
                continue
            if budgeted and incomplete_budget:
                row.update(status="rejected", reasons=["existing_budget_unverifiable"])
                continue

            in_main = any(s.etf == etf for s in main_sectors)
            in_sub = any(s.etf == etf for s in sub_sectors)
            chain_state = chain_map.get(c.chain_id)
            chain_hot = chain_state.hot if chain_state else False

            # 放行判定（v6.0：与回测共用 gate.pass_gates 单一实现）
            decision = pass_gates(mrs.mrs_star, shs, c.tss_final,
                                  in_main, in_sub, chain_hot)
            if not decision.passed:
                row.update(status="rejected", reasons=gate_reasons(
                    mrs.mrs_star, shs, c.tss_final, in_main=in_main,
                    in_sub=in_sub, chain_hot=chain_hot))
                continue
            standard = decision.standard

            # ---- S4 合规围栏前置（D2）：市场规则校验先于仓位计算 ----
            entry = c.price
            sector_row = next((s for s in sectors if s.etf == etf), None)
            breadth = sector_row.breadth if sector_row is not None else None
            no_chase = (isinstance(breadth, (int, float)) and math.isfinite(breadth)
                        and breadth < config.BREADTH_NO_CHASE)
            if budgeted and no_chase:
                if not math.isfinite(c.key_level) or c.key_level <= 0:
                    row.update(status="rejected", reasons=["sector_chase_evidence_missing"])
                    continue
                if entry > c.key_level:
                    row.update(status="rejected", reasons=["sector_breadth_no_chase"])
                    continue
            verdict = None
            if spec is not None:
                sdf = context["market_data"].get("stock_ohlcv", {}).get(c.ticker)
                prev_close = (float(sdf["Close"].iloc[-2])
                              if sdf is not None and len(sdf) >= 2 else entry)
                verdict = spec.check_order("buy", c.ticker, entry, prev_close,
                                           context.get("trade_date"))
                compliance.append({"ticker": c.ticker, "side": "buy",
                                   **verdict.to_dict()})
                if not verdict.allowed:
                    row.update(status="rejected", reasons=["compliance_rejected"])
                    notes.append(f"合规拒绝：{c.ticker} {verdict.reason}"
                                 f"（{verdict.rule_id}，围栏前置）")
                    continue

            size_ratio = 1.0 if standard else sum(light_probe["size_ratio"]) / 2
            # D7 轻仓通道：未达标市场强制轻仓 ×0.3-0.4（覆盖标准/轻仓通道）
            if grad_forced:
                size_ratio = round(sum(config.MARKET_LIGHT_SIZE) / 2, 4)

            # v6.0 个股事件折扣：财报 1-2 天内 → 仓位 ×0.5（白皮书§11.1）
            event_note = ""
            if cal:
                ev_factor, event_note = cal.pick_adjustment(c.ticker, trade_date)
                size_ratio = round(size_ratio * ev_factor, 4)

            # TOS 排序分（归一化 0-10）
            c.tos = round(mrs.mrs_star * shs * c.tss_final * c.c_liq / 100, 2)

            # 保留评分、原币价格和合规判定；不能用 USD 风险预算除原币价差。
            if currency_policy["execution_blocked"]:
                row.update(status="rejected", reasons=["currency_blocked"])
                continue

            # R 仓位反推（v6.0：结构化止损价优先，正则仅作历史数据兜底）
            stop = c.stop_price if c.stop_price > 0 else self._stop_price(c)
            if budgeted and (not all(math.isfinite(v) for v in (entry, stop, c.tss_final, mrs.mrs_star, shs))
                           or not 0 < stop < entry):
                row.update(status="rejected", reasons=["invalid_entry_or_stop"])
                continue
            risk_per_share = abs(entry - stop)
            if risk_per_share <= 0:
                row.update(status="rejected", reasons=["invalid_stop_distance"])
                continue
            r_usd = self.account * config.RISK_R_PCT * size_ratio
            shares = int(r_usd / risk_per_share)
            if shares <= 0:
                row.update(status="rejected", reasons=["risk_budget_below_one_share"])
                continue
            position_usd = shares * entry
            position_pct = position_usd / self.account
            capped = False
            if position_pct > config.MAX_SINGLE_POSITION_PCT:
                capped = True
                shares = int(self.account * config.MAX_SINGLE_POSITION_PCT / entry)
                position_usd = shares * entry
                position_pct = position_usd / self.account
            if budgeted and shares <= 0:
                row.update(status="rejected", reasons=["position_budget_below_one_share"])
                continue

            # v6.0 ATR 档位化时间止损（白皮书§10.3 资金效率口径）
            time_stop = next((d for cap_atr, d in config.TIME_STOP_BY_ATR
                              if c.atr_pct <= cap_atr), config.TIME_STOP_DAYS[1])

            mode = "标准做多" if standard else "轻仓试错"
            if grad_forced:
                mode += (f"·新市场轻仓（验证期 {grad.get('settled', 0)}"
                         f"/{grad.get('required')}）")
            rationale[c.ticker] = {
                "mode": mode, "standard": bool(standard),
                "in_main": bool(in_main), "in_sub": bool(in_sub),
                "chain_hot": bool(chain_hot), "shs": shs, "etf": etf,
                "mrs_star": mrs.mrs_star, "mrs_light": bool(mrs_light),
                "tss_final": c.tss_final,
                "size_ratio": round(size_ratio, 4),
                "event_note": event_note,
                "account": self.account, "r_pct": config.RISK_R_PCT,
                "r_usd": round(r_usd, 2),
                "entry": round(entry, 2), "stop": round(stop, 2),
                "risk_per_share": round(risk_per_share, 4),
                "shares": shares, "position_pct": round(position_pct, 4),
                "position_capped": capped,
                "max_single_pct": config.MAX_SINGLE_POSITION_PCT,
                "time_stop_days": time_stop,
                "tos": c.tos,
                "sector_evidence": evidence,
                "risk_group": risk_group(c.chain_id, etf),
                **({"signal_adv_usd": c.adv_usd} if budgeted else {}),
                **({"entry_setup": {"template": c.entry_template,
                    "sector_breadth": breadth, "no_chase": no_chase,
                    "entry_ceiling": c.key_level if no_chase else None}}
                   if budgeted else {}),
                # S4：市场合规与轻仓通道毕业状态（全透传，报告层直接消费）
                "market_grad": dict(grad),
                "compliance_rule": verdict.rule_id if verdict is not None else "",
                # 三门 ok 口径必须与放行判定严格一致（理论§5，可审计性红线）：
                # 轻仓试错通道下，MRS 轻仓区（5.5-6.0）或次主线池（SHS 7.0-7.5）
                # 即为对应门的合法通过路径，否则会出现"被放行但门未过"的记录矛盾。
                "gate": {
                    "mrs": {"value": mrs.mrs_star, "threshold": config.OPEN_LONG["mrs"],
                            "threshold_probe_light": light_probe["mrs_lo"],
                            "ok": bool(mrs.mrs_star >= config.OPEN_LONG["mrs"]
                                       or (decision.passed and not standard and
                                           (mrs_light or (budgeted and in_sub and not in_main)))),
                            **({"probe_channel": "mrs_light" if mrs_light else
                                "qualified_sub_sector" if in_sub and not in_main else "none"}
                               if budgeted else {})},
                    "shs": {"value": shs, "main_pool": bool(in_main),
                            "hot_channel": bool(chain_hot and shs >= config.SHS_SUB_POOL),
                            "threshold_main": config.SHS_MAIN_POOL,
                            "threshold_hot": config.SHS_SUB_POOL,
                            "ok": bool(in_main
                                       or (chain_hot and shs >= config.SHS_SUB_POOL)
                                       or (decision.passed and not standard and in_sub))},
                    "tss": {"value": c.tss_final, "threshold": config.OPEN_LONG["tss"],
                            "threshold_probe": light_probe["tss"],
                            "ok": c.tss_final >= (config.OPEN_LONG["tss"] if standard
                                                  else light_probe["tss"])},
                },
            }
            card = self._trade_card(c, mrs, shs, entry, stop, shares, size_ratio, mode)
            if event_note:
                card += f"\n  事件折扣: {event_note}"
            picks.append(TradePick(
                ticker=c.ticker, tss_final=c.tss_final, tos=c.tos,
                entry_template=c.entry_template, entry_price=round(entry, 2),
                stop_price=round(stop, 2), shares=shares,
                position_pct=round(position_pct, 4), risk_usd=round(r_usd, 0),
                chain=c.chain_id, sector=etf, card=card,
                time_stop_days=time_stop, event_note=event_note,
            ))
            row.update(status="provisional", reasons=[])

        picks.sort(key=lambda p: p.tos, reverse=True)
        max_new = max(0, self.max_picks - len(existing_tickers)) if budgeted else self.max_picks
        if not budgeted:
            for discarded in picks[max_new:]:
                diagnostics[discarded.ticker].update(status="rejected", reasons=["position_slots_exhausted"])
            picks = picks[:max_new]

        if budgeted:
            # Budget the whole account, including previous holdings and pending
            # signals. Scale a legal plan to remaining budget instead of raising
            # a score/risk threshold or silently dropping a partially fundable plan.
            used_gross, used_groups = existing_gross, dict(existing_groups)
            funded = []
            for pick in picks:
                if len(funded) >= max_new:
                    diagnostics[pick.ticker].update(status="rejected", reasons=["position_slots_exhausted"])
                    continue
                group = risk_group(pick.chain, pick.sector)
                risk_per_share = abs(pick.entry_price - pick.stop_price)
                max_chain = max(0.0, self.account * config.MAX_CHAIN_RISK_PCT - used_groups.get(group, 0.0))
                max_gross = max(0.0, (gross_cap - used_gross) * self.account)
                permitted = min(pick.shares, int(max_chain / risk_per_share),
                                int(max_gross / pick.entry_price))
                if permitted <= 0:
                    reason = "group_risk_budget_exhausted" if max_chain < risk_per_share else "gross_budget_exhausted"
                    diagnostics[pick.ticker].update(status="rejected", reasons=[reason])
                    continue
                pick.shares = permitted
                pick.position_pct = round(permitted * pick.entry_price / self.account, 6)
                pick.risk_usd = round(permitted * risk_per_share, 6)
                used_groups[group] = used_groups.get(group, 0.0) + pick.risk_usd
                used_gross += permitted * pick.entry_price / self.account
                detail = rationale[pick.ticker]
                detail.update(shares=permitted, position_pct=pick.position_pct,
                              r_usd=pick.risk_usd, risk_group=group)
                pick.card = self._trade_card(
                    next(c for c in candidates if c.ticker == pick.ticker), mrs,
                    detail["shs"], pick.entry_price, pick.stop_price, permitted,
                    detail["size_ratio"], detail["mode"])
                funded.append(pick)
            picks = funded

        # ---- v6.0 组合层风控（白皮书§9/§10：仓位是风险预算的机器执行）----
        # 1) 产业链敞口上限：单链累计风险 ≤ MAX_CHAIN_RISK_PCT×账户
        #    （一条链的 N 个"独立 0.8%R"实际是同一个 R——链级利空会同时引爆）
        chain_risk: dict[str, float] = {}
        chain_limited: list[TradePick] = []
        for p in picks:
            budget = self.account * config.MAX_CHAIN_RISK_PCT
            used = chain_risk.get(p.chain, 0.0)
            if p.chain and used + p.risk_usd > budget:
                diagnostics[p.ticker].update(status="rejected", reasons=["group_risk_budget_exhausted"])
                notes.append(f"产业链敞口上限：{p.ticker}（{p.chain}）跳过——"
                             f"该链累计风险 ${used:,.0f}+${p.risk_usd:,.0f} 超预算 "
                             f"${budget:,.0f}（白皮书§10：单链风险集中即同一个 R）")
                continue
            chain_risk[p.chain] = used + p.risk_usd
            chain_limited.append(p)
        picks = chain_limited

        # 2) 总敞口执行：计划市值合计 ≤ Gross Cap（MRS* 上限 × 事件折扣）
        if config.ENFORCE_GROSS_CAP and gross_cap > 0:
            total, gross_limited = 0.0, []
            for p in picks:
                if total + p.position_pct > gross_cap + 1e-9:
                    diagnostics[p.ticker].update(status="rejected", reasons=["gross_budget_exhausted"])
                    notes.append(f"总仓位上限执行：{p.ticker} 跳过——计划敞口 "
                                 f"{total:.0%}+{p.position_pct:.0%} 超上限 {gross_cap:.0%}"
                                 "（白皮书§4.4：仓位是风险预算，不是观点表达）")
                    continue
                total += p.position_pct
                gross_limited.append(p)
            picks = gross_limited

        # v5.4 修复：决策依据必须与最终放行标的【一一对应】
        kept = {p.ticker for p in picks}
        for ticker in kept:
            diagnostics[ticker].update(status="accepted", reasons=[])
        rationale = {t: info for t, info in rationale.items() if t in kept}

        if mrs.mrs_star < config.MRS_GATE_LIGHT:
            action, view = "HOLD", "轻仓试探区 — 只做最高质量结构，严格止损，减少隔夜"
        else:
            action, view = "BUY", "可按推荐仓位建仓（分批 40/40/20）"
        if not picks:
            action, view = "WAIT", "无达标标的 — 宁可错过，不可做错（理论§1.3）"
        if currency_policy["execution_blocked"]:
            action, view = "WAIT", currency_policy["reason"]

        context["action"] = action
        context["market_view"] = view
        context["picks"] = picks
        context["notes"] = notes
        context["pick_rationale"] = rationale
        context["gross_cap"] = gross_cap
        context["compliance"] = compliance
        context["rejections"] = rejection_report(list(diagnostics.values()))
        if budgeted:
            context["entry_budget"] = {"schema": "tiger.entry-budget.v1", "gross_cap": gross_cap,
                "single_cap": config.MAX_SINGLE_POSITION_PCT,
                "group_risk_pct": config.MAX_CHAIN_RISK_PCT,
                "risk_pct": config.RISK_R_PCT, "max_positions": self.max_picks,
                "known_at": trade_date, "existing_gross_reserved": existing_gross}
        self.log.info("风控放行: action=%s picks=%s", action, [p.ticker for p in picks])
        return picks

    # ------------------------------------------------------------

    @staticmethod
    def _stop_price(c: StockCandidate) -> float:
        """从 stop_plan 中还原止损价（由 TSS 层生成，含“参考 xx.xx”）。"""
        import re
        m = re.search(r"参考\s*([\d.]+)", c.stop_plan or "")
        if m:
            return float(m.group(1))
        return c.price * 0.95

    @staticmethod
    def _trade_card(c: StockCandidate, mrs: MRSResult, shs: float,
                    entry: float, stop: float, shares: int,
                    size_ratio: float, mode: str) -> str:
        """交易计划卡（投资者业务语言；内部评分参数不外显）。"""
        risk_pct = abs(entry - stop) / entry * 100
        tpl = {"A": "突破后回踩确认", "B": "收缩后放量启动",
               "C": "趋势回撤企稳"}.get(c.entry_template, c.entry_template or "待定")
        return (
            f"【交易计划】{c.ticker}（{mode}）\n"
            f"  方向归属: {c.sector_etf or 'N/A'} 板块（热度 {shs}/10）｜ "
            f"{chain_name_zh(c.chain_id) if c.chain_id else 'N/A'} 产业链\n"
            f"  市场环境: 综合 {mrs.mrs_star}/10（{mrs.regime}）\n"
            f"  入场形态: {tpl} ｜ 个股综合质量 {c.tss_final}/10\n"
            f"  关键位: {c.key_level:.2f} ｜ 入场参考: {entry:.2f} ｜ 止损: {stop:.2f}（-{risk_pct:.1f}%，跌破无条件离场）\n"
            f"  计划股数: {shares} 股\n"
            f"  加仓纪律: 结构确认后加二仓（再创高且不放异常巨量）\n"
            f"  时间纪律: 入场后 5-7 个交易日未推进或跑输板块 → 降仓/换股\n"
            f"  盈利保护: 浮盈达到两倍风险后，止损上移至成本线/最近结构支撑\n"
            f"  证伪条件: {c.stop_plan}"
        )
