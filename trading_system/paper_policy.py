"""Signal-time budgets and next-open fills for both strict paper profiles.

The fill calculation consumes only the admitted signal's budget and actual
opening prices. Today's closing research cannot cancel or authorize an earlier
opening fill. Existing positions can drift above limits; no new entry expands
such a breach. Stop budgets do not guarantee execution through a price gap.
"""
from __future__ import annotations

from datetime import date
import math

from . import config
from .activity import risk_group
from .calendar import next_trading_day


def _positive(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def freeze_entry_budget(result, pick, *, execution_date: date | None = None) -> dict:
    from .simulator import friction_bps
    raw = result.raw
    limits = raw.get("entry_budget")
    if not isinstance(limits, dict) or limits.get("schema") != "tiger.entry-budget.v1":
        raise ValueError("Activity paper signals require an explicit signal-time budget")
    gross = limits.get("gross_cap")
    if not _positive(gross) or gross > max(row[3] for row in config.MRS_POSITION_CAP):
        raise ValueError("Invalid activity signal gross budget")
    detail = (raw.get("pick_rationale") or {}).get(pick.ticker, {})
    if pick.entry_template not in {"A", "B", "C"}:
        raise ValueError("Strict paper signal has no recognized entry setup")
    setup = detail.get("entry_setup")
    if (not isinstance(setup, dict) or type(setup.get("no_chase")) is not bool
            or (setup["no_chase"] and not _positive(setup.get("entry_ceiling")))):
        raise ValueError("Strict paper signal has no valid no-chase evidence")
    ratio = detail.get("size_ratio")
    if not _positive(ratio) or ratio > 1:
        raise ValueError("Activity signal has no valid risk-size ratio")
    adv = detail.get("signal_adv_usd")
    if isinstance(adv, bool) or not isinstance(adv, (int, float)) or not math.isfinite(adv) or adv < 0:
        raise ValueError("Activity signal has no valid signal-time ADV observation")
    signal_date = date.fromisoformat(result.trade_date)
    return {"schema": "tiger.pending-budget.v1", "known_at": signal_date.isoformat(),
            "execution_date": (execution_date or next_trading_day(signal_date)).isoformat(),
            "gross_cap": gross, "single_cap": config.MAX_SINGLE_POSITION_PCT,
            "risk_pct": config.RISK_R_PCT * ratio,
            "group_risk_pct": config.MAX_CHAIN_RISK_PCT,
            "max_positions": config.MAX_PICKS_DEFAULT,
            "risk_group": risk_group(pick.chain, pick.sector), "entry_template": pick.entry_template,
            "no_chase": setup["no_chase"], "entry_ceiling": setup["entry_ceiling"],
            "signal_adv_usd": adv, "entry_friction_bps": friction_bps(adv)}


def frozen_entry_friction(pending: dict) -> float | None:
    """Only signal-time volume/close can select an opening cost tier."""
    from .simulator import friction_bps
    budget = pending.get("entry_budget")
    if not isinstance(budget, dict):
        return None
    adv, bps = budget.get("signal_adv_usd"), budget.get("entry_friction_bps")
    if (isinstance(adv, bool) or not isinstance(adv, (int, float))
            or not math.isfinite(adv) or adv < 0 or isinstance(bps, bool)
            or not isinstance(bps, (int, float)) or not math.isfinite(bps)
            or bps != friction_bps(adv)):
        return None
    return bps


def opening_allocation(state: dict, pending: dict, bar, bar_date: str,
                       dated_bar, fill_net: float) -> dict:
    """Return a complete executable bound or a stable refusal code; never mutate."""
    budget = pending.get("entry_budget")
    refused = lambda code: {"shares": 0, "reason": code}
    if (not isinstance(budget, dict) or budget.get("schema") != "tiger.pending-budget.v1"
            or budget.get("known_at") != pending.get("signal_date")):
        return refused("signal_budget_missing")
    if frozen_entry_friction(pending) is None:
        return refused("signal_budget_invalid")
    if budget.get("entry_template") not in {"A", "B", "C"}:
        return refused("entry_template_missing")
    if (type(budget.get("no_chase")) is not bool
            or (budget["no_chase"] and not _positive(budget.get("entry_ceiling")))):
        return refused("signal_budget_invalid")
    if budget["no_chase"] and (bar is None or bar.open > budget["entry_ceiling"]):
        return refused("sector_breadth_no_chase")
    if type(pending.get("shares")) is not int or pending["shares"] <= 0:
        return refused("signal_quantity_invalid")
    try:
        signal_date = date.fromisoformat(pending["signal_date"])
        if pending.get("signal_id"):
            from .market_close import next_scheduled_close
            expected = next_scheduled_close(signal_date).date().isoformat()
        else:
            expected = next_trading_day(signal_date).isoformat()
    except (KeyError, TypeError, ValueError):
        return refused("signal_date_invalid")
    if budget.get("execution_date") != expected:
        return refused("signal_budget_date_invalid")
    if bar_date != expected:
        return refused("execution_window_missed" if bar_date > expected else "execution_not_due")
    for name, maximum in (("single_cap", config.MAX_SINGLE_POSITION_PCT),
                          ("risk_pct", config.RISK_R_PCT),
                          ("group_risk_pct", config.MAX_CHAIN_RISK_PCT),
                          ("gross_cap", max(row[3] for row in config.MRS_POSITION_CAP))):
        if not _positive(budget.get(name)) or budget[name] > maximum + 1e-12:
            return refused("signal_budget_invalid")
    if (type(budget.get("max_positions")) is not int
            or not 1 <= budget["max_positions"] <= config.MAX_PICKS_DEFAULT
            or not isinstance(budget.get("risk_group"), str) or not budget["risk_group"]):
        return refused("signal_budget_invalid")
    if any(position["ticker"] == pending["ticker"] for position in state["positions"]):
        return refused("already_held")
    if len(state["positions"]) >= budget["max_positions"]:
        return refused("position_slots_exhausted")
    if not _positive(fill_net) or not _positive(pending.get("stop_price")) or fill_net <= pending["stop_price"]:
        return refused("invalid_entry_or_stop")
    gross, groups = 0.0, {}
    for position in state["positions"]:
        observed = dated_bar(position["ticker"])
        if observed is None or observed[1] != bar_date or not _positive(observed[0].open):
            return refused("opening_holdings_valuation_unavailable")
        gross += position["shares"] * observed[0].open
        group = position.get("risk_group") or risk_group(position.get("chain", ""), position.get("sector", ""))
        groups[group] = groups.get(group, 0.0) + position["shares"] * max(
            0.0, position["entry_price"] - position["stop"])
    equity = state["cash"] + gross
    if not _positive(equity):
        return refused("opening_equity_invalid")
    risk_per_share = fill_net - pending["stop_price"]
    group = budget["risk_group"]
    available_gross = max(0.0, equity * budget["gross_cap"] - gross)
    available_group_risk = max(0.0, equity * budget["group_risk_pct"] - groups.get(group, 0.0))
    quantities = {
        "signal_quantity": pending["shares"],
        "cash": int(max(0.0, state["cash"]) / fill_net),
        "single_position": int(equity * budget["single_cap"] / fill_net),
        "single_risk": int(equity * budget["risk_pct"] / risk_per_share),
        "gross": int(available_gross / fill_net),
        "group_risk": int(available_group_risk / risk_per_share),
    }
    shares = min(quantities.values())
    if shares <= 0:
        return refused("opening_" + min(quantities, key=quantities.get) + "_budget_exhausted")
    cost = shares * fill_net
    risk = shares * risk_per_share
    gross_after = (gross + cost) / equity
    single_after = cost / equity
    group_after = (groups.get(group, 0.0) + risk) / equity
    if (cost > state["cash"] + 1e-7 or gross_after > budget["gross_cap"] + 1e-10
            or single_after > budget["single_cap"] + 1e-10
            or risk / equity > budget["risk_pct"] + 1e-10
            or group_after > budget["group_risk_pct"] + 1e-10):
        raise AssertionError("Activity opening allocation violated an admitted budget")
    return {"shares": shares, "reason": "accepted", "opening_equity": equity,
            "positions_after": len(state["positions"]) + 1, "duplicate": False,
            "entry_price": fill_net,
            "entry_friction_cost": shares * (fill_net - round(bar.open, 2)) if bar is not None else None,
            "gross_after": gross_after, "single_after": single_after,
            "group_risk_after": group_after, "risk_after": risk / equity,
            "risk_usd": risk, "risk_group": group, "budget": budget}
