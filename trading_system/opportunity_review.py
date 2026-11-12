"""Review research opportunities in an existing, independently verified Tiger run."""

import argparse
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from statistics import mean
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .agent_evidence import digest, read_bytes, read_snapshot, strict_json, verify_ref
from .agent_runtime import verify_run

_NY = ZoneInfo("America/New_York")
_SETUPS = {"A", "B", "C"}
_LIMIT = 5
_REVIEW_LIMIT = 6
_REASONS = {
    "entry_template_missing": "缺少入场形态",
    "mrs_below_standard": "市场评分低于标准开仓线",
    "no_legal_probe_channel": "不满足轻仓通道条件",
    "sector_outside_standard_channel": "板块不满足标准开仓条件",
    "tss_below_probe": "个股评分低于轻仓线",
    "tss_below_standard": "个股评分低于标准开仓线",
}


def _positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def _observations(frame, session):
    columns = frame["columns"]
    close, high, low, volume = (columns.index(key) for key in ("Close", "High", "Low", "Volume"))
    dates = [date.fromisoformat(row["value"][:10]) for row in frame["index"][-21:]]
    values = frame["values"]
    if (len(values) != len(frame["index"]) or len(dates) < 21 or dates[-1] != session
            or any(a >= b for a, b in zip(dates, dates[1:]))):
        raise ValueError("Expected an ordered 21-session window ending on the report date")
    recent = [(row[close], row[high], row[low], row[volume]) for row in values[-21:]]
    if any(not all(_positive(v) for v in row) or not row[2] <= row[0] <= row[1] for row in recent):
        raise ValueError("Invalid prices or volumes in the report window")
    last = recent[-1]
    return dates, {
        "close": last[0],
        "day_pct": 100 * (last[0] / recent[-2][0] - 1),
        "return_5_pct": 100 * (last[0] / recent[-6][0] - 1),
        "return_20_pct": 100 * (last[0] / recent[0][0] - 1),
        "relative_volume": last[3] / mean(row[3] for row in recent[:-1]),
    }


def _stock_metrics(market, symbol, session, benchmark_dates, benchmark):
    dates, values = _observations(market["stock_ohlcv"][symbol], session)
    if dates != benchmark_dates:
        raise ValueError("Stock and benchmark sessions differ")
    return {"excess_20_pp": values["return_20_pct"] - benchmark["return_20_pct"], **values}


def _market_review(result, market):
    freshness = result["raw"]["freshness"]["market_close"]
    cutoff = datetime.fromisoformat(freshness["close_at_utc"])
    session = date.fromisoformat(result["trade_date"])
    if (cutoff.utcoffset() is None or cutoff.astimezone(_NY).date() != session
            or freshness["session"] != str(session)):
        raise ValueError("The result lacks a bound U.S. market close")
    benchmark_dates, benchmark = _observations(market["spy"], session)
    universe = result["raw"]["research_audit"]["funnel"]
    rejected = {row["ticker"]: row["reasons"] for row in result["raw"]["rejections"]["candidates"]}
    cards, unavailable, seen = {}, [], set()
    for candidate in result["watchlist"]:
        symbol = candidate["ticker"]
        if symbol in seen:
            raise ValueError(f"Duplicate symbol: {symbol}")
        seen.add(symbol)
        try:
            values = _stock_metrics(market, symbol, session, benchmark_dates, benchmark)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            unavailable.append(f"{symbol} ({exc})")
            continue
        key, atr, tss = (candidate[field] for field in ("key_level", "atr_pct", "tss_final"))
        if not all(_positive(v) for v in (key, atr, tss)):
            unavailable.append(f"{symbol} (缺少关键位、ATR 或评分)")
            continue
        if abs(values["close"] - candidate["price"]) > 0.011:
            raise ValueError(f"Frozen close differs from the scored price for {symbol}")
        cards[symbol] = {
            "symbol": symbol, "setup": candidate["entry_template"] or None,
            "tss": tss, "key": key,
            "key_gap_atr": (values["close"] - key) / (values["close"] * atr),
            "risk_reasons": rejected[symbol], **values,
        }
    extra, extra_missing = [], []
    for row in universe["rows"]:
        symbol = row["ticker"]
        if symbol in seen or row["gates"]["hard_filter"]["status"] != "passed":
            continue
        try:
            values = _stock_metrics(market, symbol, session, benchmark_dates, benchmark)
        except (KeyError, TypeError, ValueError, IndexError):
            extra_missing.append(symbol)
            continue
        if values["excess_20_pp"] > 0 and values["relative_volume"] >= 1.5:
            extra.append({"symbol": symbol, **values})
    return {
        "cutoff": cutoff.astimezone(_NY), "action": result["action"], "buys": len(result["picks"]),
        "universe": universe["universe_size"], "quotes": universe["gates"]["market_data"]["passed"],
        "hard_pass": universe["gates"]["hard_filter"]["passed"],
        "deep_count": len(result["watchlist"]), "unavailable": unavailable,
        "benchmark": benchmark, "cards": cards,
        "extra": sorted(extra, key=lambda c: (-c["excess_20_pp"], c["symbol"])),
        "extra_missing": extra_missing,
    }


def _nonempty_text(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n<>"):
        raise ValueError("Expected one nonempty line of plain text")
    return value


def _validate_review(review, market, available):
    if review["schema"] != "tiger.issuer-review.v1" or review["market_date"] != str(market["cutoff"].date()):
        raise ValueError("Issuer review date or schema does not match the verified session")
    rows = review["candidates"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= _REVIEW_LIMIT:
        raise ValueError("Issuer review must contain one to six reviewed candidates")
    seen = set()
    for row in rows:
        symbol = row["symbol"]
        if symbol not in available or symbol in seen:
            raise ValueError(f"Missing or duplicate verified candidate: {symbol}")
        seen.add(symbol)
        for key in ("thesis", "risk", "confirmation", "invalidation"):
            _nonempty_text(row[key])
        if not isinstance(row["sources"], list) or not row["sources"]:
            raise ValueError(f"No reviewed company sources for {symbol}")
        for source in row["sources"]:
            _nonempty_text(source["title"])
            raw_url = _nonempty_text(source["url"])
            url = urlsplit(raw_url)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or any(c.isspace() or c in "()\\" for c in raw_url)):
                raise ValueError(f"Invalid company source URL for {symbol}")
            published = date.fromisoformat(source["published_on"])
            if "published_at" in source:
                instant = datetime.fromisoformat(source["published_at"])
                if (instant.utcoffset() is None or instant.astimezone(_NY).date() != published
                        or instant > market["cutoff"]):
                    raise ValueError(f"Unverified publication time or post-close source for {symbol}")
            elif published >= market["cutoff"].date():
                raise ValueError(f"Same-day sources require an exact publication time for {symbol}")
    return rows


def _research_queue(cards):
    setups = sorted((c for c in cards.values() if c["setup"] in _SETUPS),
                    key=lambda c: (-c["tss"], c["symbol"]))
    near = sorted((c for c in cards.values() if c["setup"] not in _SETUPS
                   and abs(c["key_gap_atr"]) <= 1 and c["excess_20_pp"] > 0),
                  key=lambda c: (-c["tss"], -c["excess_20_pp"], c["symbol"]))
    return (setups[:1] + near + setups[1:])[:_LIMIT]


def _render(market, verified, run_id, review=None):
    receipt_hash = verified["verification"]["receipt_sha256"]
    if review is not None and (not isinstance(review, dict)
                               or (review.get("run_id"), review.get("receipt_sha256")) != (run_id, receipt_hash)):
        raise ValueError("Issuer review does not belong to this exact verified run")
    available = {**market["cards"], **{c["symbol"]: c for c in market["extra"]}}
    rows = _validate_review(review, market, available) if review is not None else []
    selected = [available[row["symbol"]] for row in rows] if rows else _research_queue(market["cards"])
    cutoff = market["cutoff"]
    lines = [
        f"# Tiger 潜力股研究｜{cutoff.date()} 美股收盘",
        "",
        f"研究时点为纽约 {cutoff:%Y-%m-%d %H:%M %Z}。原交易判断为 **{market['action']}**，"
        f"买入计划 {market['buys']} 个。以下是收盘时点的研究优先级。",
        "",
        f"股票池 {market['universe']} 只，有可用行情 {market['quotes']} 只，"
        f"通过基础筛选 {market['hard_pass']} 只，精评 {market['deep_count']} 只。"
        f"其中 {len(market['cards'])} 只能按共同交易日复算量价指标。",
        f"SPY 同期 5 日 {market['benchmark']['return_5_pct']:+.2f}%，"
        f"20 日 {market['benchmark']['return_20_pct']:+.2f}%。",
        "",
        (f"**研究复核顺位 {'、'.join(c['symbol'] for c in selected)}。**" if rows else
         "**待取证名单。** 优先覆盖最高分的有效形态，再按精评分数排列接近关键位且跑赢 SPY 的个股。"
         "公司证据尚待核对。"),
        "",
        "| 顺位 | 股票 | 收盘 / 关键位（美元） | 5 日 / 20 日涨跌 | 20 日相对 SPY | 当日相对量 | 形态 / TSS |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, c in enumerate(selected, 1):
        key = f"{c['key']:.2f}" if "key" in c else "—"
        shape = f"{c['setup'] or '待确认'} / {c['tss']:.2f}" if "tss" in c else "扩围复核 / —"
        lines.append(
            f"| {i} | {c['symbol']} | {c['close']:.2f} / {key} | "
            f"{c['return_5_pct']:+.2f}% / {c['return_20_pct']:+.2f}% | "
            f"{c['excess_20_pp']:+.2f} 个百分点 | {c['relative_volume']:.2f}× | "
            f"{shape} |")
    if not selected:
        lines.extend(["", "当前没有符合研究队列条件的标的。"])
    if market["unavailable"]:
        lines.extend(["", "量价口径无法复算：" + "、".join(market["unavailable"]) + "。"])
    lines.extend(["", "相对量取当日成交量除以前 20 个交易日的均量。关键位和 TSS 来自原运行。"
                  "量价指标使用封存的行情，本工具不另行刷新报价。"])
    for i, row in enumerate(rows, 1):
        c = available[row["symbol"]]
        setup = c.get("setup")
        status = [setup + " 形态" if setup else "无 A/B/C 形态"] if "tss" in c else ["未进入原精评"]
        status.extend(_REASONS.get(reason, reason) for reason in c.get("risk_reasons", [])
                      if reason != "entry_template_missing")
        lines.extend([
            "", f"## {i}. {row['symbol']}", "", row["thesis"], "",
            f"- **复核条件**　{row['confirmation']}",
            f"- **失效信号**　{row['invalidation']}",
            f"- **主要风险**　{row['risk']}",
            f"- **原规则状态**　{'，'.join(status)}。",
        ])
        sources = " · ".join(f"[{s['title']}](<{s['url']}>)（{s['published_on']}）"
                             for s in row["sources"])
        lines.append(f"- **人工复核来源**　{sources}")
    checked = market["hard_pass"] - market["deep_count"] - len(market["extra_missing"])
    lines.extend([
        "", "## 精评名单之外的量价线索", "",
        f"另有 {checked} 只通过基础筛选的股票完成同口径复算。"
        f"其中 {len(market['extra'])} 只近 20 日跑赢 SPY 且当日相对量至少 1.50 倍。"
        "下表按相对收益排序，仍待逐股复核形态与公司证据。",
    ])
    reviewed = {row["symbol"] for row in rows}
    extra = [c for c in market["extra"] if c["symbol"] not in reviewed]
    if extra:
        lines.extend([
            "", "| 股票 | 收盘（美元） | 当日 / 近 5 日 | 20 日相对 SPY | 当日相对量 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for c in extra[:_LIMIT]:
            lines.append(f"| {c['symbol']} | {c['close']:.2f} | {c['day_pct']:+.2f}% / {c['return_5_pct']:+.2f}% | "
                         f"{c['excess_20_pp']:+.2f} 个百分点 | {c['relative_volume']:.2f}× |")
    if market["extra_missing"]:
        lines.extend(["", "该范围内无法复算：" + "、".join(market["extra_missing"]) + "。"])
    lines.extend([
        "", f"原运行 {run_id}。独立验真回执 SHA-256 {receipt_hash}。",
    ])
    if rows:
        lines.append("公司事实和排序由研究复核输入提供，程序检查标的、资料格式和发布时间边界。")
    lines.append("")
    return "\n".join(lines)


def build_report(state_root, run_id, issuer_review=None):
    verified = verify_run(state_root, run_id, current_source=None)
    if verified["status"] != "completed":
        raise ValueError("Opportunity review requires a completed, independently verified run")
    run = state_root / "runs" / run_id
    receipt_bytes = read_bytes(run, "receipt.json")
    if digest(receipt_bytes) != verified["verification"]["receipt_sha256"]:
        raise ValueError("Receipt changed after verification")
    receipt = strict_json(receipt_bytes)
    refs = [ref for ref in receipt["artifacts"] if ref["path"].startswith("work/reports/result_")]
    if len(refs) != 1:
        raise ValueError("Expected one sealed result")
    result = strict_json(verify_ref(run, refs[0], schema="application/json"))
    ref = result["raw"]["research_audit"]["evidence_refs"]["scan_universe"]
    snapshot = read_snapshot(run, ref, run_id=run_id, stage="layer0.scan", role="input")
    review = None if issuer_review is None else strict_json(issuer_review)
    report = _render(_market_review(result, snapshot["market_data"]), verified, run_id, review)
    if issuer_review is not None:
        report += f"复核输入 SHA-256 {digest(issuer_review)}。\n"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path, help="Absolute path to Tiger state")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--issuer-review", type=Path)
    args = parser.parse_args(argv)
    try:
        review = args.issuer_review.read_bytes() if args.issuer_review else None
        report = build_report(args.state_root, args.run_id, review)
    except (OSError, TypeError, ValueError, KeyError, IndexError) as exc:
        parser.exit(2, f"机会复核失败：{exc}\n")
    print(report, end="")


if __name__ == "__main__":
    main()
