"""小虎模拟盘记账引擎（老虎全球资产管理 · 全AI掌控模拟盘）。

定位：公开验证 AI 投资能力的 Paper Trading 台账。每日跟随系统全链路产出记账：
  - 信号节拍：T 日收盘后产生信号 → **T+1 日开盘价成交**（无未来函数，
    公开战绩抗质疑）；
  - 出场纪律：止损（盘中触及按止损价，跳空低开按开盘价）/ 时间止损
    （7 个交易日未推进）/ 盈利保护（浮盈 ≥2R 止损上移至成本线）；
  - AVOID 日：禁止新开仓（pending 不落），现持仓继续按出场纪律管理；
    HOLD（轻仓试探区）日出战的轻仓信号照常登记——与 journal 落账口径互为镜像
    （白皮书 §9.1：HOLD=轻仓试探区"只做最高质量结构"；§15.2：小G 与 journal 镜像）；
  - 零基线兼容：台账 sim_portfolio.json 属会计账（白名单保留），
    行情一律当日实时拉取，绝不复用历史缓存。

台账 JSON 结构（全可序列化）：
  cash / positions[] / pending[] / closed[] / equity_curve[] / ops_log[]
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date

from . import config
from .currency import ACCOUNT_CURRENCY, require_usd_result, require_usd_sim_state
from .ledger_io import write_json_atomic

log = logging.getLogger(__name__)

TIME_STOP_DAYS = 7          # 时间止损：7 个交易日未推进
PROFIT_PROTECT_R = 2.0      # 盈利保护：浮盈 ≥2R 止损移成本线
SIM_FILENAME = "sim_portfolio.json"


@dataclass
class Bar:
    """单根日 K（来自四环行情链的最新完整交易日）。

    adv: 20 日平均成交额（USD），用于 ADV 分档滑点（v6.3 摩擦模型）；
    adv≤0 表示未知（旧台账/外部注入），回落 v6.0 合并口径 COST_BPS。
    date: 行情本身的交易日期（YYYY-MM-DD），未知日期的行情不得执行成交。
    """
    open: float
    high: float
    low: float
    close: float
    adv: float = 0.0
    date: str | None = None


def friction_bps(adv: float) -> float:
    """单边摩擦成本（bp）= ADV 分档滑点 + 单边佣金（v6.3，防滑点断崖）。

    - adv > 0：按 config.SIM_SLIPPAGE_BY_ADV 分档（ADV 越小滑点越大）
      + config.SIM_COMMISSION_BPS（与 v6.0 回测净口径一致的单边 10bp）；
    - adv ≤ 0（未知）：回落 v6.0 合并口径 config.COST_BPS（10bp），
      保证旧台账与未携带 ADV 的 Bar 口径不变。
    """
    if adv and adv > 0:
        for lo, slip in config.SIM_SLIPPAGE_BY_ADV:
            if adv >= lo:
                return slip + config.SIM_COMMISSION_BPS
    return config.COST_BPS


class SimEngine:
    def __init__(self, path: str, initial_cash: float = 100_000.0):
        self.path = path
        self.initial_cash = initial_cash
        loaded = self._load()
        self.state = loaded if loaded is not None else {
            "started": None, "initial_cash": initial_cash,
            "account_currency": ACCOUNT_CURRENCY,
            "cash": initial_cash, "positions": [], "pending": [],
            "closed": [], "equity_curve": [], "ops_log": [],
        }
        self.initial_cash = self.state["initial_cash"]

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError(f"模拟盘账本 {self.path} JSON 损坏，已停止以保留原账本") from exc
        required = {"started", "initial_cash", "cash", "positions", "pending",
                    "closed", "equity_curve", "ops_log"}
        if (not isinstance(state, dict) or not required.issubset(state)
                or any(not isinstance(state[key], list)
                       for key in ("positions", "pending", "closed", "equity_curve", "ops_log"))
                or state["started"] is not None and not isinstance(state["started"], str)
                or any(isinstance(state[key], bool)
                       or not isinstance(state[key], (int, float))
                       or not math.isfinite(state[key])
                       for key in ("initial_cash", "cash"))):
            raise ValueError(f"模拟盘账本 {self.path} 结构无效，已停止以保留原账本")
        require_usd_sim_state(state, scope=f"模拟盘账本 {self.path}")
        return state

    def save(self) -> None:
        require_usd_sim_state(self.state, scope=f"模拟盘账本 {self.path}")
        write_json_atomic(self.path, self.state, indent=1)

    # ---------------------------------------------------------------- 主节拍
    def step(self, trade_date: str, result, get_bar, max_new: int = 8, *,
             paper=None, run_id: str | None = None, references: dict | None = None,
             persist: bool = True) -> dict:
        """每日记账：先结算昨日信号（今日开盘价成交）→ 出场纪律 → 记净值
        → 登记今日新信号（明日成交）。

        result: PipelineResult（今日全链路产出）
        get_bar: callable(ticker) -> Bar | None（最新完整日 K，四环链实时拉取）
        """
        # 两项检查都先于 started/现金/持仓/行情调用，失败保留内存与原账本。
        require_usd_result(result, scope="模拟盘输入")
        require_usd_sim_state(self.state, scope=f"模拟盘账本 {self.path}")
        s = self.state
        if paper is None and s.get("paper_contract"):
            raise ValueError("Authoritative paper accounts require their execution contract")
        s["started"] = s["started"] or trade_date
        ops: list[str] = []
        # v6.5 来源留痕：每条成交/持仓记录数据来源（合成数据统一记 "demo"），
        # 让"合成数据是否进过公开台账"在事后可机器判定，而不是靠信任。
        from .providers.base import source_label
        from .activity import result_enabled
        budgeted = result_enabled(result)
        if budgeted:
            from .activity import risk_group
            from .paper_policy import freeze_entry_budget, frozen_entry_friction, opening_allocation
        facts = None
        if paper is not None:
            from .market_close import next_scheduled_close
            from .paper_ledger import PaperFacts
            if not budgeted or not run_id or not references:
                raise ValueError("Authoritative paper execution requires frozen approvals and budgets")
            if s["ops_log"] and s["ops_log"][-1]["date"] > trade_date:
                raise ValueError("Paper session cannot precede the committed account")
            facts = PaperFacts(s, run_id, references)
            session = date.fromisoformat(trade_date)
            opening_already_closed = s.get("paper_last_closed_open", "") >= trade_date
            if paper.completed_session >= session:
                from datetime import timedelta
                through = session - timedelta(days=1)
            else:
                through = paper.completed_session
            self._manage_completed(paper, through, ops, facts)
        source = source_label(getattr(result, "provider", ""))
        allow_new = (result.action != "AVOID" and
                     getattr(getattr(result, "mrs", None), "allow_new_positions", True))
        bar_cache: dict[str, tuple[Bar, str] | None] = {}
        fill_checks = []

        def dated_bar(ticker: str) -> tuple[Bar, str] | None:
            """本轮每票只读一次；未知/未来日期不能借运行日补造成交日期。"""
            if ticker not in bar_cache:
                bar = get_bar(ticker)
                observed = None
                if bar is not None:
                    try:
                        bar_date = date.fromisoformat(bar.date).isoformat()
                    except (TypeError, ValueError):
                        ops.append(f"⏸️ {ticker} 日K日期缺失或无效，等待可核实行情")
                    else:
                        if bar_date <= trade_date:
                            observed = (bar, bar_date)
                        else:
                            ops.append(f"⏸️ {ticker} 日K日期 {bar_date} 晚于运行日，等待可核实行情")
                bar_cache[ticker] = observed
            return bar_cache[ticker]

        def open_bar(ticker):
            return paper.opening(ticker, session) if paper is not None else dated_bar(ticker)

        def refuse(pending, reason, *, kind="rejected", bar=None, record_check=True):
            if record_check:
                fill_checks.append({"ticker": pending["ticker"], "date": trade_date,
                                    "shares": 0, "reason": reason})
            if facts is not None:
                facts.emit(pending.get("signal_id"), kind, trade_date, reason=reason, bar=bar)

        # ---------- 1. 昨日信号成交（T+1 开盘价，无未来函数） ----------
        still_pending = []
        for p in s["pending"]:
            if not budgeted and not allow_new:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 今日禁止新开仓，待成交信号顺延")
                continue
            if not p.get("signal_date") or p["signal_date"] >= trade_date:
                still_pending.append(p)          # 当日信号次日才成交（同日重跑幂等）
                continue
            if paper is not None:
                signal = facts.signals.get(p.get("signal_id"))
                if signal is None:
                    refuse(p, "legacy_signal_unbound")
                    ops.append(f"⏸️ {p['ticker']} 历史信号缺执行证据，释放待成交预算")
                    continue
                expected = signal["planned_open_session"]
                if trade_date > expected:
                    refuse(p, "SIGNAL_EXPIRED", kind="expired")
                    ops.append(f"⏸️ {p['ticker']} 已错过指定开盘，信号过期")
                    continue
                if trade_date < expected or opening_already_closed or not paper.opening_ready(session):
                    still_pending.append(p)
                    continue
            if budgeted:
                if not isinstance(p.get("entry_budget"), dict):
                    refuse(p, "signal_budget_missing")
                    ops.append(f"⏸️ {p['ticker']} 缺少原信号冻结预算，释放待成交预算")
                    continue
                from .calendar import next_trading_day
                expected = next_trading_day(date.fromisoformat(p["signal_date"])).isoformat()
                if paper is None and trade_date > expected:
                    fill_checks.append({"ticker": p["ticker"], "date": trade_date,
                                        "shares": 0, "reason": "execution_window_missed"})
                    ops.append(f"⏸️ {p['ticker']} 已错过信号指定开盘，释放待成交预算")
                    continue
            observed = open_bar(p["ticker"])
            if observed is None:
                if facts is not None:
                    refuse(p, "BAR_UNAVAILABLE", kind="opening_unavailable")
                    ops.append(f"⏸️ {p['ticker']} 指定开盘行情不可用，等待本会话证据")
                still_pending.append(p)          # 无行情，顺延一日
                continue
            bar, bar_date = observed
            if bar_date <= p["signal_date"] or bar.open <= 0:
                still_pending.append(p)          # 必须是信号之后的真实交易日
                continue
            fill = round(bar.open, 2)
            if facts is not None and p["entry_ref"] > 0 and abs(fill / p["entry_ref"] - 1) > 0.35:
                refuse(p, "corporate_action_or_extreme_open_gap", bar=bar)
                ops.append(f"⚠️ {p['ticker']} 开盘与信号价偏离超过 35%，待核对公司行动")
                continue
            if fill <= p["stop_price"]:
                if facts is not None:
                    refuse(p, "opening_gap_through_stop", bar=bar)
                ops.append(f"⚠️ {p['ticker']} 开盘价直接跌破止损 {p['stop_price']:.2f}，"
                           "风险模型失效，信号作废")
                continue
            risk_ps = abs(fill - p["stop_price"])
            if risk_ps <= 0:
                continue
            shares = min(p["shares"], int(p["risk_usd"] / risk_ps))
            # v6.3：成交价 = 开盘价 × (1 + 单边摩擦)——ADV 分档滑点 + 单边佣金
            # （保守摩擦口径，防滑点断崖；ADV 未知时回落 v6.0 合并 10bp）
            bps = frozen_entry_friction(p) if budgeted else friction_bps(bar.adv)
            if bps is None:
                refuse(p, "signal_budget_invalid", bar=bar)
                ops.append(f"⏸️ {p['ticker']} 信号时点成本证据无效，释放待成交预算")
                continue
            fill_net = round(fill * (1 + bps / 10_000), 4)
            cost = shares * fill_net
            allocation = None
            if budgeted:
                allocation = opening_allocation(s, p, bar, bar_date, open_bar, fill_net)
                fill_checks.append({"ticker": p["ticker"], "date": bar_date, **allocation})
                if not allocation["shares"]:
                    if facts is not None and allocation["reason"] == "opening_holdings_valuation_unavailable":
                        refuse(p, "BAR_UNAVAILABLE", kind="opening_unavailable", record_check=False)
                        still_pending.append(p)
                        ops.append(f"⏸️ {p['ticker']} 缺少持仓开盘估值，等待本会话证据")
                        continue
                    refuse(p, allocation["reason"], bar=bar, record_check=False)
                    # Missing the one admitted opening is never repaired by
                    # inventing a later fill at an unrelated day's opening.
                    ops.append(f"⏸️ {p['ticker']} 公开模拟开盘拒绝：{allocation['reason']}")
                    # A known opening refusal is terminal. Retrying after this
                    # session's exits would use later freed cash/slots at an
                    # earlier opening and manufacture an executable history.
                    continue
                shares = allocation["shares"]
                cost = shares * fill_net
            if shares <= 0 or cost > s["cash"]:
                if facts is not None:
                    refuse(p, "opening_risk_or_cash_exhausted", bar=bar)
                ops.append(f"⚠️ {p['ticker']} 开盘价漂移后风险/现金超限，放弃该信号")
                continue
            # v6.0 总敞口执行（白皮书§4.4）：计划投入后敞口超 MRS* 上限则顺延
            cap = (result.mrs.position_cap[1] if getattr(result, "mrs", None) else 1.0)
            invested_now = sum(x["shares"] * x["entry_price"] for x in s["positions"])
            equity_est = s["cash"] + invested_now
            if not budgeted and cap < 1.0 and equity_est > 0 and (invested_now + cost) / equity_est > cap:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 敞口将达 {(invested_now + cost) / equity_est:.0%}"
                           f" 超上限 {cap:.0%}，顺延（仓位是风险预算，不是观点表达）")
                continue
            s["cash"] = round(s["cash"] - cost, 2)
            s["positions"].append({
                "ticker": p["ticker"], "shares": shares, "entry_price": fill_net,
                "stop": p["stop_price"], "entry_date": bar_date,
                "chain": p.get("chain", ""), "sector": p.get("sector", ""),
                "risk_usd": (allocation["risk_usd"] if allocation else round(shares * risk_ps, 2)),
                "time_stop_days": p.get("time_stop_days", 0),
                "peak_price": fill, "note": p.get("note", ""),
                "source": source,
                # v6.3 三栏台账：毛口径入场价与入场摩擦档位（出场摩擦按当时 ADV 重定档）
                "entry_raw": fill, "friction_bps": bps,
                "bars_held": 0,
                **({"signal_id": p["signal_id"], "last_price": fill_net} if facts else {}),
                **({"risk_group": allocation["risk_group"], "entry_budget": allocation["budget"],
                    "opening_allocation": allocation} if allocation else {}),
            })
            if facts is not None:
                facts.emit(p["signal_id"], "filled", bar_date, bar=bar, fill={
                    "entry": fill_net, "entry_raw": fill, "shares": shares,
                    "friction_cost": round(shares * (fill_net - fill), 2),
                    "risk_usd": s["positions"][-1]["risk_usd"],
                })
            ops.append(f"🟢 买入 {p['ticker']} {shares} 股 @ {fill:.2f}"
                       f"（开盘价成交，止损 {p['stop_price']:.2f}）")
        s["pending"] = still_pending

        # ---------- 2. 出场纪律 ----------
        if paper is not None:
            self._manage_completed(paper, paper.completed_session, ops, facts)
            s["paper_last_closed_open"] = paper.completed_session.isoformat()
        else:
            self._manage_positions(dated_bar, ops, source, budgeted)

        # ---------- 3. 记净值 ----------
        equity = s["cash"]
        marks: dict[str, float] = {}
        for pos in s["positions"]:
            px = pos.get("last_price", pos["entry_price"])
            marks[pos["ticker"]] = px
            equity += pos["shares"] * px
        equity = round(equity, 2)
        if not s["equity_curve"] or s["equity_curve"][-1]["date"] != trade_date:
            s["equity_curve"].append({"date": trade_date, "equity": equity})
        else:
            s["equity_curve"][-1]["equity"] = equity

        # ---------- 4. 登记今日新信号（明日开盘价成交） ----------
        # v3 镜像修复（2026-08-30 真实运行暴露）：旧口径仅 BUY/LIGHT 日登记，
        # 导致 HOLD（轻仓试探区）日 journal 落账 4 笔而 sim 按兵不动——两本账
        # 不再镜像（白皮书 §15.2）。L4 闸门已把五态语义编码进 picks 本身，
        # 故 HOLD/BUY/LIGHT 日只要存在放行 picks 即与 journal 同口径登记；
        # 唯独 AVOID 是白皮书绝对禁止项（MRS*<4 不开新仓），防御性拒绝登记。
        if result.picks and allow_new:
            held = ({p["ticker"] for p in s["positions"]}
                    | {p["ticker"] for p in s["pending"]})
            added = 0
            for pick in result.picks[:max_new]:
                if pick.ticker in held:
                    continue                      # 同名标的已有敞口，不重复登记
                pending = {
                    "ticker": pick.ticker, "shares": pick.shares,
                    "stop_price": pick.stop_price, "risk_usd": pick.risk_usd,
                    "chain": pick.chain, "sector": pick.sector,
                    "signal_date": trade_date,
                    "time_stop_days": getattr(pick, "time_stop_days", 0) or 0,
                    "source": source,
                    "note": f"{pick.entry_template or '标准'}｜质量 {pick.tss_final}/10",
                    **({"entry_ref": pick.entry_price,
                        "entry_budget": freeze_entry_budget(result, pick, execution_date=(
                            next_scheduled_close(session).date() if facts else None)),
                        "risk_group": risk_group(pick.chain, pick.sector)} if budgeted else {}),
                }
                if facts is not None:
                    sid = facts.register(pending, result, pick, trade_date, paper.earnings_dates)
                    if sid is None:
                        continue
                    pending["signal_id"] = sid
                s["pending"].append(pending)
                held.add(pick.ticker)
                added += 1
            if added:
                ops.append(f"📋 新信号 {added} 只登记在册，"
                           "明日开盘价成交（无未来函数）")
        if not ops:
            ops.append("😴 今日按兵不动——纪律优先，空仓也是一种仓位")

        entry = {"date": trade_date, "ops": ops, "equity": equity, "source": source}
        if s["ops_log"] and s["ops_log"][-1]["date"] == trade_date:
            s["ops_log"][-1] = entry               # 同日重跑幂等覆盖
        else:
            s["ops_log"].append(entry)
        if persist:
            self.save()
        result = {"equity": equity, "ops": ops, "marks": marks,
                  **({"fill_checks": fill_checks} if budgeted else {})}
        if facts is not None:
            open_status = "available_window" if paper.opening_ready(session) else "SESSION_NOT_OPEN"
            if opening_already_closed:
                open_status = "SESSION_ALREADY_SEALED"
            result.update(events=facts.new, observation_status={
                "open": open_status,
                "completed": "complete" if paper.completed_session == session else "SESSION_NOT_COMPLETE",
                "latest_completed_session": paper.completed_session.isoformat(),
                "missing": list(facts.missing.values()),
            })
        return result

    def _manage_completed(self, paper, through, ops, facts):
        from datetime import timedelta
        from .paper_ledger import required_sessions

        while self.state["positions"]:
            observed = {}
            for pos in self.state["positions"]:
                ticker = pos["ticker"]
                pos.setdefault("last_price", pos["entry_price"])
                if not pos.get("signal_id") and "bars_held" not in pos and self.state["equity_curve"]:
                    prior = min(through.isoformat(), self.state["equity_curve"][-1]["date"])
                    if prior >= pos["entry_date"]:
                        pos.setdefault("last_bar_date", prior)
                        pos["bars_held"] = _trading_days(
                            pos["entry_date"], pos["last_bar_date"], self.state["equity_curve"])
                after = (date.fromisoformat(pos["last_bar_date"]) if pos.get("last_bar_date")
                         else date.fromisoformat(pos["entry_date"]) - timedelta(days=1))
                session = next(required_sessions(after, through), None)
                if session is None:
                    continue
                value = paper.completed(ticker, session)
                if value is None:
                    facts.emit(pos.get("signal_id"), "completed_unavailable", session.isoformat(),
                               reason="BAR_UNAVAILABLE")
                    continue
                observed[ticker] = value
            if not observed:
                return
            self._manage_positions(observed.get, ops, None, True, facts=facts)

    def _manage_positions(self, dated_bar, ops, source, budgeted, *, facts=None):
        s = self.state
        kept = []
        for pos in s["positions"]:
            last_bar_date = pos.get("last_bar_date")
            if facts is None and last_bar_date is None and "bars_held" not in pos and s["equity_curve"]:
                # 旧台账的保守水位只迁移一次，避免后续记账日不断向前推水位，
                # 导致早晨取得的昨日 K 永远被误判为已经处理。
                last_bar_date = max(e["date"] for e in s["equity_curve"])
                pos.update(last_bar_date=last_bar_date,
                           bars_held=_trading_days(pos["entry_date"], last_bar_date,
                                                   s["equity_curve"]))
            observed = dated_bar(pos["ticker"])
            if observed is None:
                kept.append(pos)
                continue
            bar, bar_date = observed
            if "last_price" not in pos and bar_date >= pos["entry_date"]:
                # 旧台账即使不重放出场，也应保留有效收盘估值，不能退回成本价。
                pos["last_price"] = bar.close
            if (bar_date < pos["entry_date"]
                    or last_bar_date is not None and bar_date <= last_bar_date):
                kept.append(pos)
                continue
            # v6.0：出场判定统一调用 exit_engine.evaluate_day（与 journal/回测同一套
            # 规则语义，含 ATR 档位化时间止损）；卖出按净价（v6.3 ADV 分档摩擦）。
            from .exit_engine import evaluate_day
            # 新持仓只按不同的真实日 K 累计，周末/陈旧行情重跑不增加持仓日数。
            # 旧台账首次迁移保留已有记账日计数，此后同样按实际 K 线推进。
            days = (pos["bars_held"] + 1 if "bars_held" in pos else
                    _trading_days(pos["entry_date"], bar_date, s["equity_curve"]))
            act = evaluate_day(bar.open, bar.high, bar.low, bar.close,
                               pos["entry_price"], pos["stop"], days,
                               time_stop=pos.get("time_stop_days") or TIME_STOP_DAYS)
            exit_price, reason = None, ""
            if act.action == "exit":
                exit_price, reason = act.exit_price, act.reason
            elif act.action == "protect":
                pos["stop"] = pos["entry_price"]
                if facts is not None:
                    facts.emit(pos.get("signal_id"), "protected", bar_date, reason="profit_protection", bar=bar)
                ops.append(f"🛡️ {pos['ticker']} 浮盈达 2R，止损上移至成本线 {pos['stop']:.2f}")
            if exit_price is not None:
                # v6.3：卖出净价 = 毛价 × (1 − 单边摩擦)；出场 ADV 未知时沿用入场档位
                exit_raw = exit_price
                bps_out = (friction_bps(bar.adv) if bar.adv and bar.adv > 0
                           else float(pos.get("friction_bps") or friction_bps(0.0)))
                exit_price = round(exit_raw * (1 - bps_out / 10_000), 4)
                proceeds = exit_price * pos["shares"]
                pnl = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
                r_mult = round(pnl / max(pos["risk_usd"], 1e-9), 2)
                # v6.3 三栏台账：毛收益 / 摩擦成本 / 净收益（恒等式 毛−摩擦=净）
                entry_raw = float(pos.get("entry_raw") or pos["entry_price"])
                gross_pnl = round((exit_raw - entry_raw) * pos["shares"], 2)
                friction = max(0.0, round(gross_pnl - pnl, 2))   # 摩擦永不为负
                gross_r = round(gross_pnl / max(pos["risk_usd"], 1e-9), 2)
                s["cash"] = round(s["cash"] + proceeds, 2)
                closed = {
                    "ticker": pos["ticker"], "entry_date": pos["entry_date"],
                    "exit_date": bar_date, "entry": pos["entry_price"],
                    "exit": exit_price, "shares": pos["shares"],
                    "pnl_usd": pnl, "r_multiple": r_mult, "reason": reason,
                    "days": days,
                    "gross_pnl": gross_pnl, "gross_r": gross_r,
                    "friction_cost": friction, "net_r": r_mult,
                    "source": pos.get("source", "legacy") if facts is not None else source,
                    **({"signal_id": pos["signal_id"]} if facts is not None and "signal_id" in pos else {}),
                    **({"entry_raw": entry_raw, "exit_raw": exit_raw,
                        "entry_friction_cost": (pos["entry_price"] - entry_raw) * pos["shares"],
                        "exit_friction_cost": (exit_raw - exit_price) * pos["shares"]} if budgeted else {}),
                }
                s["closed"].append(closed)
                if facts is not None:
                    facts.emit(pos.get("signal_id"), "closed", bar_date, reason=reason, bar=bar, close=closed)
                ops.append(f"🔴 卖出 {pos['ticker']} {pos['shares']} 股 @ {exit_price:.2f}"
                           f"（{reason}，{'盈' if pnl >= 0 else '亏'} ${abs(pnl):,.0f}，{r_mult}R）")
            else:
                pos["peak_price"] = max(pos["peak_price"], bar.high)
                pos.update(last_bar_date=bar_date, bars_held=days, last_price=bar.close)
                kept.append(pos)
        s["positions"] = kept

    # ---------------------------------------------------------------- 统计
    def stats(self) -> dict:
        require_usd_sim_state(self.state, scope=f"模拟盘账本 {self.path}")
        s = self.state
        closed = s["closed"]
        n = len(closed)
        wins = [c for c in closed if c["pnl_usd"] > 0]
        gross_w = sum(c["pnl_usd"] for c in wins)
        gross_l = abs(sum(c["pnl_usd"] for c in closed if c["pnl_usd"] <= 0))
        curve = [e["equity"] for e in s["equity_curve"]]
        peak, max_dd = self.initial_cash, 0.0
        for e in curve:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak if peak else 0.0)
        equity = curve[-1] if curve else self.initial_cash
        # v6.3 三栏汇总：毛收益 / 摩擦成本 / 净收益（旧台账缺字段时摩擦按 0 计）
        gross_pnl = round(sum(c.get("gross_pnl", c["pnl_usd"]) for c in closed), 2)
        net_pnl = round(sum(c["pnl_usd"] for c in closed), 2)
        friction_total = round(sum(c.get("friction_cost", 0.0) for c in closed), 2)
        return {
            "equity": equity, "cash": s["cash"],
            "invested": round(equity - s["cash"], 2),
            "cum_return": (equity / self.initial_cash - 1.0) if self.initial_cash else 0.0,
            "pnl_gross": gross_pnl, "pnl_net": net_pnl,
            "friction_total": friction_total,
            "n_closed": n, "win_rate": (len(wins) / n) if n else None,
            "expectancy_r": (sum(c["r_multiple"] for c in closed) / n) if n else None,
            "profit_factor": (round(gross_w / gross_l, 2) if gross_l > 0 else None) if n else None,
            "max_drawdown": max_dd, "days": len(s["equity_curve"]),
        }


def _trading_days(d0: str, d1: str, curve: list[dict]) -> int:
    """持仓交易日数：用净值曲线日期序列数（真实记账日）。"""
    dates = [e["date"] for e in curve]
    try:
        return max(1, len([d for d in dates if d0 < d <= d1]))
    except Exception:
        return 1
