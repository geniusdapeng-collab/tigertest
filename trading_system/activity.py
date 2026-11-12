"""Versioned evidence repairs; no score, data-quality or risk threshold changes."""
from __future__ import annotations

from collections import Counter
import math

from . import config
from .chains import CHAINS
from .sector_classification import OBSERVED_DATE, SECTOR_PROXIES, SOURCE_URL

ACTIVE_PROFILE = "us-public-active-paper-v1"
ACTIVE_POLICY_ID = "tiger.public-active-paper-policy.v1"


def enabled(context: dict) -> bool:
    return context.get("activity_profile") == ACTIVE_PROFILE


def execution_budgeted(context: dict) -> bool:
    return enabled(context) or context.get("paper_execution_budgeted") is True


def result_enabled(result) -> bool:
    return execution_budgeted(getattr(result, "raw", {}))


def sector_evidence(ticker: str, chain_id: str, *, active: bool) -> dict:
    """Prefer Tiger's reviewed thematic chain; use only frozen explicit proxies."""
    if chain_id in CHAINS:
        return {"etf": CHAINS[chain_id]["etf"], "source": "tiger_thematic_chain",
                "chain_id": chain_id}
    proxy = SECTOR_PROXIES.get(ticker) if active else None
    if proxy is not None:
        return {**proxy, "source": "nasdaq_sector_proxy", "observed_date": OBSERVED_DATE,
                "source_url": SOURCE_URL}
    return {"etf": "", "source": "unmapped"}


def assign_pools(sectors: list, *, active: bool) -> None:
    """Preserve v1 decisions; active overflow retains the unchanged sub threshold."""
    main_count = 0
    for sector in sectors:
        sector.in_main_pool = sector.in_sub_pool = False
        eligible = (sector.shs >= config.SHS_MAIN_POOL
                    and math.isfinite(sector.breadth)
                    and sector.breadth >= config.BREADTH_HEALTHY)
        if eligible:
            if main_count < config.MAIN_POOL_MAX:
                sector.in_main_pool = True
                main_count += 1
            elif active:
                sector.in_sub_pool = True
                sector.evidence.append("主线名额已满；保留已达原阈值的次主线轻仓资格")
        elif sector.shs >= config.SHS_SUB_POOL:
            sector.in_sub_pool = True
            if sector.shs >= config.SHS_MAIN_POOL and not math.isfinite(sector.breadth):
                sector.evidence.append(
                    f"{sector.etf} SHS≥{config.SHS_MAIN_POOL} 但广度数据缺失 → "
                    "不进主线池（主线入池需广度≥60%证据），降次主线")


def gate_reasons(mrs: float, shs: float, tss: float, *, in_main: bool,
                 in_sub: bool, chain_hot: bool) -> list[str]:
    """Overlapping reasons explain both existing legal channels deterministically."""
    reasons = []
    if mrs < config.OPEN_LONG["mrs"]:
        reasons.append("mrs_below_standard")
    if not (in_main or (chain_hot and shs >= config.SHS_SUB_POOL)):
        reasons.append("sector_outside_standard_channel")
    if tss < config.OPEN_LONG["tss"]:
        reasons.append("tss_below_standard")
    if not (config.LIGHT_PROBE["mrs_lo"] <= mrs < config.OPEN_LONG["mrs"]
            or (in_sub and not in_main)):
        reasons.append("no_legal_probe_channel")
    if tss < config.LIGHT_PROBE["tss"]:
        reasons.append("tss_below_probe")
    return reasons


def risk_group(chain: str, sector: str) -> str:
    return chain or (f"sector:{sector}" if sector else "unmapped")


def portfolio_reservations(state: dict | None, account: float) -> tuple[float, dict, set, bool]:
    """Reserve existing and pending exposure; unknown pending value blocks new plans."""
    state = state or {}
    gross, groups, tickers, incomplete = 0.0, {}, set(), False
    for pending, records in ((False, state.get("positions", [])),
                             (True, state.get("pending", []))):
        for record in records:
            tickers.add(record["ticker"])
            shares = record["shares"]
            price = record.get("entry_ref") if pending else record.get("last_price", record.get("entry_price"))
            if (isinstance(shares, bool) or not isinstance(shares, (int, float))
                    or not math.isfinite(shares) or shares <= 0 or isinstance(price, bool)
                    or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0):
                incomplete = True
                continue
            gross += shares * price / account
            group = record.get("risk_group") or risk_group(record.get("chain", ""), record.get("sector", ""))
            risk = (record.get("risk_usd", 0) if pending else
                    shares * max(0.0, record["entry_price"] - record["stop"]))
            groups[group] = groups.get(group, 0.0) + risk
    return gross, groups, tickers, incomplete


def rejection_report(rows: list[dict]) -> dict:
    primary = Counter(row["status"] for row in rows)
    reasons = Counter(reason for row in rows for reason in row.get("reasons", []))
    return {"schema": "tiger.rejections.v1", "candidate_count": len(rows),
            "status_counts": dict(sorted(primary.items())),
            "overlapping_reason_counts": dict(sorted(reasons.items())), "candidates": rows}
