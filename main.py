#!/usr/bin/env python3
"""老虎交易系统（Tiger Trading）— 主入口。

运行模式（--mode）：
  daily      日频全流程：扫描 → 三道门+ICS → 风控 → 日报 + 信号落账/结算（默认）
  premarket  盘前：全流程 + 开盘作战计划 + 昨日信号结算
  intraday   盘中：基于最新日报 picks 做价位触发监控（--interval/--cycles）

独立工具：
  --backtest  无未来函数回测（--bt-days 信号日数）
  --tune      滚动 WFA 调参 + DSR 校正 → tuned_params.json（显著才覆盖默认）

示例：
  python main.py --demo                          # 离线演示
  python main.py --universe full                 # 请求所选市场列表，实际范围见报告
  python main.py --mode premarket --universe full
  python main.py --mode intraday --interval 300 --cycles 12
  python main.py --backtest --demo --bt-days 260
  python main.py --tune --demo --bt-days 380
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
from dataclasses import dataclass

from trading_system import config
from trading_system.pipeline import run_pipeline
from trading_system.report import render_markdown, to_json, to_markdown

log = logging.getLogger(__name__)


def _looks_like_live_ledger(out_dir: str) -> list[str]:
    """列出该目录下【已含非合成记录】的台账文件（合成数据写入前的前置闸门）。

    判据：记录缺 `source` 字段、或 `source != "demo"` 即视为真实台账——
    历史台账（v6.5 之前没有 source 字段）同样按真实处理（fail-closed：
    宁可拒绝一次演示落账，也不能把合成成交混进公开战绩）。
    """
    hits: list[str] = []
    sim_path = os.path.join(out_dir, "sim_portfolio.json")
    if os.path.exists(sim_path):
        try:
            with open(sim_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = None
        if isinstance(state, dict):
            records: list = []
            for key in ("positions", "pending", "closed", "ops_log"):
                records.extend(v for v in (state.get(key) or []) if isinstance(v, dict))
            if any(rec.get("source") != "demo" for rec in records):
                hits.append("sim_portfolio.json")
    journal_path = os.path.join(out_dir, "journal.json")
    if os.path.exists(journal_path):
        try:
            with open(journal_path, encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            records = None
        if isinstance(records, list) and any(
                isinstance(rec, dict) and rec.get("source") != "demo" for rec in records):
            hits.append("journal.json")
    return hits


def _resolve_out_dir(requested: str, synthetic: bool) -> str:
    """合成数据运行的落盘目录（默认隔离到 reports/demo/）。

    只有调用方【显式】指定了别的目录（--out）才按调用方给的路径走，
    并在写入前由调用方执行 live-ledger 闸门。
    """
    # 与真实公开台账物理隔离：reports/ 下的 journal.json / sim_portfolio.json /
    # options_hist / 日报_* 都是真账，合成数据一律另起 reports/demo/。
    demo_out_dir = os.path.join(config.REPORTS_DIR, "demo")
    if synthetic and os.path.abspath(requested) == os.path.abspath(config.REPORTS_DIR):
        return demo_out_dir
    return requested


def _journal_section(stats: dict) -> str:
    if not stats or stats.get("closed", 0) == 0:
        return "## 十一、胜率追踪（信号日记）\n\n尚无已结算信号——账本从今天开始积累，" \
               "每笔放行信号将按出场规则自动结算。\n"
    lines = ["## 十一、胜率追踪（信号日记）", ""]
    lines.append(f"- 已结算 **{stats['closed']}** 笔（未结算信号 {stats['open']}）｜ "
                 f"累计 **{stats['total_r']}R**")
    lines.append(f"- 总胜率 **{stats['win_rate']:.1%}** ｜ 期望 **{stats['expectancy_r']}R** ｜ "
                 f"利润因子 {stats['profit_factor']}")
    l20, l50 = stats["last20"], stats["last50"]
    if l20["n"]:
        lines.append(f"- 近 20 笔: 胜率 {l20['win_rate']:.1%}（期望 {l20['avg_r']}R）"
                     + (f" ｜ 近 50 笔: 胜率 {l50['win_rate']:.1%}" if l50["n"] else ""))
    sf = stats.get("settle_failed")
    if sf:
        lines.append(f"- ⚠️ {len(sf)} 个标的行情缺失无法结算（悬挂待下轮）: "
                     f"{', '.join(sf[:8])}{'...' if len(sf) > 8 else ''}")
    bt = stats.get("by_template", {})
    if bt:
        lines.append("- 分模板: " + " ｜ ".join(
            f"模板{k} {v['n']}笔/胜率{v['win_rate']:.0%}/均{v['avg_r']}R"
            for k, v in bt.items() if v["n"]))
    lines.append("")
    return "\n".join(lines)


def _research_daily_reports(args, result, reason: str) -> None:
    """A blocked accounting boundary still produces a successful research run."""
    json_path = to_json(result, args.out)
    md_path = to_markdown(result, args.out)
    with open(md_path, "a", encoding="utf-8") as handle:
        handle.write("\n## 十一、账本状态\n\n" + reason
                     + "\n本轮未更新信号日记、模拟盘、复盘与治理事件；既有账本原样保留。\n")
    html_path = None
    if args.html:
        from trading_system.report_html import render_html
        html_path = os.path.join(args.out, f"日报_{result.trade_date.replace('-', '')}.html")
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(render_html(result, asset_dir=args.out))
    plan_path = None
    if args.mode == "premarket":
        plan_path = os.path.join(args.out, f"盘前计划_{result.trade_date.replace('-', '')}.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write(f"# 盘前研究 — {result.trade_date}\n\n{reason}\n\n"
                         "本轮仅提供研究，不生成开仓计划，也不执行既有仓位的模拟结算。\n")
    print(f"\n研究报告已生成（交易记账已暂停）:\n  {md_path}\n  {json_path}"
          + (f"\n  {html_path}" if html_path else "")
          + (f"\n  {plan_path}" if plan_path else "") + f"\n  {reason}\n")
    if not args.quiet:
        print(render_markdown(result))


@dataclass
class DailyRunResult:
    """Actual core output; only the independent runner can certify/publicize it."""
    result: object
    report_paths: dict[str, str]
    journal_stats: dict
    sim_state: dict
    sim_stats: dict
    sim_transition: dict
    review: dict
    governance: dict
    accounting: dict


def _daily_strict(args, provider, execution) -> DailyRunResult:
    from pathlib import Path
    from trading_system.currency import require_usd_result
    from trading_system.governance_bridge import GovernanceBridge
    from trading_system.journal import Journal
    from trading_system.ledger_io import read_json_strict, write_text_atomic
    from trading_system.pipeline import _single_with_fallback
    from trading_system.providers import get_provider
    from trading_system.providers.base import is_synthetic
    from trading_system.redline.execution import (EvidenceWriteError, ExecutionContractError,
                                                   StrictObservationError, StrictSourceSynthetic)
    from trading_system.review.chief import ReviewChief
    from trading_system.simulator import Bar, SimEngine
    from trading_system.state import purge_run_state

    paths = execution.paths

    def file_value(path):
        target = Path(path)
        if target.is_symlink() or not target.resolve().is_relative_to(paths.work_dir):
            raise ExecutionContractError("Daily artifact is outside the unique run directory")
        return {"path": str(target), "text": target.read_text(encoding="utf-8")}

    def read_file(path):
        return execution.call_io("file.read", {"path": str(path)}, lambda: file_value(path))

    def read_json(path):
        return execution.call_io("file.read_json", {"path": str(path)},
                                 lambda: read_json_strict(path))

    def write_text(path, text):
        def persist():
            write_text_atomic(path, text)
            actual = file_value(path)
            if actual["text"] != text:
                raise OSError("Report readback differs from written content")
            return actual
        return execution.call_io("file.write_text", {"path": str(path), "text": text}, persist)

    with execution.step("daily.preflight", {
        "provider": provider, "policy": execution.policy, "paths": paths.to_dict(),
        "run_date": execution.run_date,
        "request": {key: getattr(args, key, None) for key in (
            "mode", "market", "universe", "universe_file", "top", "picks",
            "account", "out", "demo", "use_tuned", "html")},
    }) as stage:
        if (args.mode != "daily" or args.market != "us" or args.demo
                or args.use_tuned or not args.html or args.universe not in ("file", "extended")
                or Path(args.out).resolve() != paths.reports_dir
                or Path.cwd().resolve() != paths.work_dir):
            raise ExecutionContractError("Daily request differs from the trusted public paper profile")
        universe_path = None
        if args.universe == "file":
            universe_path = Path(args.universe_file).resolve(strict=True)
            if not universe_path.is_relative_to(paths.work_dir):
                raise ExecutionContractError("Universe input must be staged in the unique work directory")
        elif args.universe_file is not None:
            raise ExecutionContractError("Extended universe cannot carry an ignored file override")
        settle_provider = get_provider(provider, execution=execution)
        if is_synthetic(settle_provider):
            raise StrictSourceSynthetic("Synthetic data is forbidden in the public paper workflow")
        before_journal = read_json(paths.journal_path)
        before_sim = read_json(paths.sim_path)
        before_calibration = read_json(paths.calibration_path)
        j = Journal(paths.journal_path)
        sim = SimEngine(paths.sim_path, initial_cash=args.account)
        stage.output({"provider": settle_provider.name, "synthetic": False,
                      "journal": before_journal, "sim": before_sim,
                      "calibration": before_calibration, "account_currency": "USD",
                      "universe": (read_file(universe_path) if universe_path else {"source": "frozen_extended"}),
                      "calendar": read_file(paths.calendar_override_path)})

    with execution.step("state.reset", {"work_dir": paths.work_dir}) as stage:
        purged = execution.call_io("state.purge_run_state", {"base_dir": paths.work_dir, "strict": True},
                                   lambda: purge_run_state(str(paths.work_dir), strict=True))
        stage.output(purged)

    result = run_pipeline(
        provider_name=provider, universe_mode=args.universe,
        universe_file=str(universe_path) if universe_path else None, top_n=args.top, max_picks=args.picks,
        account_usd=args.account, trade_date=execution.run_date.isoformat(),
        use_tuned=False, market="us", execution=execution,
        portfolio_state=before_sim,
    )

    with execution.step("accounting.validate", {"result": result, "journal": j.records,
                                                "sim": sim.state}) as stage:
        policy = require_usd_result(result, scope="严格日报交易记账")
        j = Journal(paths.journal_path)
        sim = SimEngine(paths.sim_path, initial_cash=args.account)
        if result.trade_date != execution.run_date.isoformat():
            raise ExecutionContractError("Pipeline result changed the frozen market date")
        result.raw["accounting"] = {**policy, "status": "pending"}
        stage.output({"policy": policy, "journal": j.records, "sim": sim.state})

    with execution.step("report.initial", {"result": result, "out_dir": paths.reports_dir}) as stage:
        json_path = execution.call_io("report.to_json", {"result": result, "out_dir": paths.reports_dir},
                                     lambda: to_json(result, str(paths.reports_dir), strict=True))
        md_path = execution.call_io("report.to_markdown", {"result": result, "out_dir": paths.reports_dir},
                                   lambda: to_markdown(result, str(paths.reports_dir), strict=True))
        stage.output({"json": read_file(json_path), "markdown": read_file(md_path)})

    with execution.step("journal.log", {"records": j.records, "result": result,
                                         "account_usd": args.account}) as stage:
        added = execution.call_io("journal.log_picks", {"records": j.records, "picks": result.picks,
                                  "trade_date": result.trade_date, "account_usd": args.account},
                                  lambda: j.log_picks(result, args.account))
        actual = read_json(paths.journal_path)
        if actual != j.records:
            raise OSError("Journal log readback differs")
        stage.output({"added": added, "records": actual})

    with execution.step("journal.settle", {"records": j.records, "as_of_date": execution.run_date,
                                            "provider": settle_provider.name}) as stage:
        settled = j.settle(settle_provider, as_of_date=execution.run_date)
        actual = read_json(paths.journal_path)
        if actual != j.records:
            raise OSError("Journal settlement readback differs")
        stage.output({"settled": settled, "records": actual, "missing_tickers": j.last_failed})

    with execution.step("journal.stats", {"records": j.records}) as stage:
        stats = j.stats()
        if j.last_failed:
            stats["settle_failed"] = list(j.last_failed)
        stage.output(stats)

    bar_cache = {}

    def get_bar(ticker):
        if ticker in bar_cache:
            return bar_cache[ticker]
        bar = None
        try:
            frame = _single_with_fallback(settle_provider, "ohlcv", ticker, days=400)
            if frame is not None and len(frame):
                # The frozen market date is the accounting cutoff. Source dates
                # remain attached to the actual bar and are never relabelled.
                frame = frame[frame.index.date <= execution.run_date]
                if len(frame):
                    row = frame.iloc[-1]
                    adv = (float((frame["Close"] * frame["Volume"]).tail(20).mean())
                           if "Volume" in frame.columns else 0.0)
                    bar = Bar(open=float(row["Open"]), high=float(row["High"]),
                              low=float(row["Low"]), close=float(row["Close"]),
                              adv=adv, date=str(frame.index[-1].date()))
        except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
            raise
        except Exception as error:
            log.warning("Strict paper bar unavailable for %s (%s); transition deferred",
                        ticker, type(error).__name__)
        bar_cache[ticker] = bar
        return bar

    with execution.step("paper.step", {"state": sim.state, "result": result,
                                       "run_date": execution.run_date}) as stage:
        sim_out = sim.step(result.trade_date, result, get_bar)
        stage.output({"transition": sim_out, "state": sim.state, "bars": bar_cache})

    with execution.step("paper.persist", {"state": sim.state, "path": paths.sim_path}) as stage:
        execution.call_io("paper.save", {"state": sim.state, "path": paths.sim_path}, sim.save)
        sim_readback = read_json(paths.sim_path)
        if sim_readback != sim.state:
            raise OSError("Paper ledger readback differs")
        result.raw["accounting"]["status"] = "updated"
        sim_stats = sim.stats()
        stage.output({"state": sim_readback, "stats": sim_stats,
                      "accounting": result.raw["accounting"]})

    with execution.step("governance.initialize", {"path": paths.governance_path}) as stage:
        before_governance = read_file(paths.governance_path)
        bridge = GovernanceBridge(str(paths.governance_path), strict=True)
        stage.output({"ledger": before_governance, "records": bridge.records(), "strict": True})

    with execution.step("review.daily", {"trade_date": result.trade_date, "records": j.records,
                                          "journal_path": paths.journal_path,
                                          "samples_path": paths.calibration_path,
                                          "proposals_dir": paths.proposals_dir}) as stage:
        chief = ReviewChief(out_dir=str(paths.reports_dir), journal_path=str(paths.journal_path),
                            proposals_dir=str(paths.proposals_dir), bridge=bridge, strict=True,
                            samples_path=str(paths.calibration_path), execution=execution)
        review_out = chief.daily(result.trade_date)
        for trace in result.raw.get("redline", []):
            if trace["step"] == "review.daily":
                trace.update(status="executed", ms=review_out["ms"], note="")
        review_memo = read_file(review_out["memo_path"])
        stage.output({"review": review_out, "memo": review_memo,
                      "calibration": read_json(paths.calibration_path),
                      "governance_event": review_out["governance_event"]})

    with execution.step("governance.emit", {"result": result, "sim_transition": sim_out}) as stage:
        emitted_before = len(bridge.emitted)
        n_events = execution.call_io("governance.emit_from_result", {"result": result, "sim_transition": sim_out},
                                    lambda: bridge.emit_from_result(result, sim_out))
        new_events = bridge.emitted[emitted_before:]
        if n_events != len(new_events):
            raise RuntimeError("Governance emission count differs from actual appended events")
        stage.output({"count": n_events, "events": new_events})

    with execution.step("governance.verify", {"path": paths.governance_path}) as stage:
        read_file(paths.governance_path)
        governance_records = bridge.records()
        ok, errors = execution.call_io("governance.verify_chain", {"records": governance_records},
                                       lambda: GovernanceBridge.verify_chain(bridge.path))
        if not ok:
            raise RuntimeError("Strict governance chain verification failed")
        governance = {"path": bridge.path, "count": n_events, "ok": ok, "errors": errors,
                      "events": governance_records}
        stage.output(governance)

    with execution.step("report.final_json", {"result": result}) as stage:
        json_path = execution.call_io("report.to_json", {"result": result, "out_dir": paths.reports_dir},
                                     lambda: to_json(result, str(paths.reports_dir), strict=True))
        stage.output(read_file(json_path))

    with execution.step("report.plan", {"mode": args.mode, "result": result}) as stage:
        # The first supported operation is daily. A real, executed mode decision
        # is evidence; no extra trading plan is silently introduced.
        stage.output({"enabled": True, "mode": args.mode, "path": None,
                      "plan": {"action_summary": result.raw["action_summary"],
                               "execution_decisions": result.raw["execution_decisions"]}}
                     if execution.model_required else
                     {"enabled": False, "mode": args.mode, "path": None, "plan": None})

    with execution.step("report.markdown", {"result": result, "journal_stats": stats}) as stage:
        markdown_text = render_markdown(result) + "\n" + _journal_section(stats)
        stage.output(write_text(md_path, markdown_text))

    frozen_benchmark = execution.signal_benchmark_chart
    chart_symbols = ["SPY"] if frozen_benchmark is not None else ["QQQ", "SPY"]
    with execution.step("report.html", {"result": result, "journal_stats": stats,
                                        "journal_entries": j.records, "sim_state": sim.state,
                                        "sim_stats": sim_stats, "benchmarks": chart_symbols}) as stage:
        from trading_system.report_html import render_html
        benchmarks = {"SPY": frozen_benchmark} if frozen_benchmark is not None else {}
        for symbol in chart_symbols if frozen_benchmark is None else ():
            try:
                frame = _single_with_fallback(settle_provider, "ohlcv", symbol, days=400)
                if frame is not None and len(frame):
                    frame = frame[frame.index.date <= execution.run_date]
                    benchmarks[symbol] = [[str(day.date()), round(float(close), 2)]
                                          for day, close in zip(frame.index, frame["Close"])]
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception as error:
                log.warning("Strict report benchmark unavailable for %s (%s)",
                            symbol, type(error).__name__)
        html_text = render_html(result, stats, j.records,
                                sim={"state": sim.state, "stats": sim_stats}, bench=benchmarks)
        html_path = paths.reports_dir / f"日报_{result.trade_date.replace('-', '')}.html"
        stage.output({"benchmarks": benchmarks, "html": write_text(html_path, html_text)})

    report_paths = {"json": str(json_path), "markdown": str(md_path),
                    "html": str(html_path), "review": review_out["memo_path"]}
    with execution.step("daily.result", {"result": result, "report_paths": report_paths,
                                         "journal_stats": stats, "sim_state": sim.state,
                                         "sim_transition": sim_out, "review": review_out,
                                         "accounting": result.raw["accounting"]}) as stage:
        reports = {name: read_file(path) for name, path in report_paths.items()}
        if (reports["markdown"]["text"] != markdown_text or reports["html"]["text"] != html_text
                or reports["review"] != review_memo):
            raise OSError("Final report readback differs from the post-review rendering")
        from trading_system.report import project_report
        expected_json, missing = project_report(result)
        expected_json.setdefault("raw", {})["serialization"] = {
            "schema": "tiger.report-json.v1", "nonfinite_values": missing,
        }
        if read_json(json_path) != expected_json:
            raise OSError("Final JSON differs from the actual post-review result")
        output = DailyRunResult(result=result, report_paths=report_paths, journal_stats=stats,
                                sim_state=sim.state, sim_stats=sim_stats,
                                sim_transition=sim_out, review=review_out,
                                governance=governance, accounting=result.raw["accounting"])
        stage.output({"daily": output, "reports": reports,
                      "journal": read_json(paths.journal_path), "paper": read_json(paths.sim_path)})
    execution.assert_complete()
    return output


def _daily(args, provider, *, execution=None):
    if execution is not None:
        return _daily_strict(args, provider, execution)
    from trading_system.currency import CurrencyBoundaryError, require_usd_result
    from trading_system.journal import Journal
    from trading_system.providers import get_provider
    from trading_system.providers.base import is_synthetic
    from trading_system.simulator import Bar, SimEngine
    from trading_system.state import purge_run_state

    # ---- 合成数据隔离闸门（v6.5）：demo 运行绝不写真实公开台账 ----
    # 触发路径：docs/QUICKSTART.md 第一条命令 `python3 main.py --demo`；修复前它会把
    # 合成价格的成交写进 reports/sim_portfolio.json（公开验证台账）与 journal.json，
    # 并在 reports/options_hist/ 留下合成期权样本（污染 TSS 期权分位）。
    synthetic = is_synthetic(get_provider(provider))
    if synthetic:
        args.out = _resolve_out_dir(args.out, True)
        live = _looks_like_live_ledger(args.out)
        if live:
            raise SystemExit(
                "红线：合成数据（demo）运行不得写入含真实记录的台账目录 "
                f"{os.path.abspath(args.out)}（命中 {', '.join(live)}）——"
                "请改用 `--out <独立目录>` 指定演示落盘位置。")
        log.warning("演示模式（合成数据）：本轮台账与报告落盘到 %s，"
                    "不写 reports/ 下的真实公开台账", os.path.abspath(args.out))

    # 校准样本在 pipeline 内写入；历史币种预检必须先于该派生账本更新。
    history_error = None
    try:
        Journal(os.path.join(args.out, "journal.json"))
        SimEngine(os.path.join(args.out, "sim_portfolio.json"), initial_cash=args.account)
    except CurrencyBoundaryError as exc:
        history_error = exc

    # 零基线纪律：每轮从零开始，先清除上一轮残留（搜索缓存/全市场清单缓存），
    # 白名单仅保留 journal.json（会计台账，不进入决策输入）。
    purged = purge_run_state()
    logging.getLogger(__name__).info("零基线清除: %s", purged)

    result = run_pipeline(
        provider_name=provider,
        universe_mode=args.universe,
        universe_file=args.universe_file,
        top_n=args.top,
        max_picks=args.picks,
        account_usd=args.account,
        use_tuned=args.use_tuned,
        market=args.market,
        accounting_blocked_reason=history_error.policy["reason"] if history_error else None,
    )
    # 先核对本轮币种与两本历史账，再做任何记账。否则先写 journal、后发现
    # sim 的历史原币记录，会留下半次运行；不能用标签替代汇率来修旧数字。
    try:
        policy = require_usd_result(result, scope="日报交易记账")
        if history_error is not None:
            raise history_error
        j = Journal(os.path.join(args.out, "journal.json"))
        sim = SimEngine(os.path.join(args.out, "sim_portfolio.json"), initial_cash=args.account)
    except CurrencyBoundaryError as exc:
        result.raw["accounting"] = {**exc.policy, "status": "blocked_currency"}
        result.picks = []
        result.raw["pick_rationale"] = {}
        if result.action != "AVOID":
            result.action = "WAIT"
        if exc.policy["reason"] not in result.market_view:
            result.market_view = ((result.market_view + "；" if result.market_view else "")
                                  + exc.policy["reason"])
        if exc.policy["reason"] not in result.notes:
            result.notes.append(exc.policy["reason"])
        for step in result.raw.get("redline", []):
            if step["step"] == "review.daily":
                step.update(status="passthrough", note=exc.policy["reason"])
        log.warning("研究结果保留，交易记账已暂停: %s", exc)
        _research_daily_reports(args, result, exc.policy["reason"])
        return
    result.raw["accounting"] = {**policy, "status": "pending"}
    json_path = to_json(result, args.out)
    md_path = to_markdown(result, args.out)

    # ---- 信号日记：落账 + 结算 + 统计 ----
    added = j.log_picks(result, args.account)
    settle_provider = get_provider(provider or ("demo" if args.demo else None))
    settled = j.settle(settle_provider)
    stats = j.stats()
    if getattr(j, "last_failed", None):
        stats["settle_failed"] = j.last_failed      # v6.1：日报披露无法结算清单
    if added or settled:
        logging.getLogger(__name__).info("信号日记: 新落账 %d，新结算 %d", added, settled)

    # ---- 小G模拟盘：初始净值遵从 --account（默认 $100,000），T+1 开盘价成交 ----
    # 台账 sim_portfolio.json 为会计账（零基线白名单保留）；行情一律当日实时拉取。
    from trading_system.pipeline import _single_with_fallback
    _bar_cache: dict = {}

    def get_bar(ticker: str):
        if ticker in _bar_cache:
            return _bar_cache[ticker]
        bar = None
        try:
            df = _single_with_fallback(settle_provider, "ohlcv", ticker, days=400)
            if df is not None and len(df):
                row = df.iloc[-1]
                # v6.3：20 日平均成交额（ADV）用于分档滑点（保守摩擦口径）
                adv = float((df["Close"] * df["Volume"]).tail(20).mean()) \
                    if "Volume" in df.columns else 0.0
                bar = Bar(open=float(row["Open"]), high=float(row["High"]),
                          low=float(row["Low"]), close=float(row["Close"]),
                          adv=adv, date=str(df.index[-1].date()))
        except Exception as exc:
            logging.getLogger(__name__).warning("小G模拟盘取 %s 日K失败（顺延）: %s", ticker, exc)
        _bar_cache[ticker] = bar
        return bar

    sim_out = sim.step(result.trade_date, result, get_bar)
    sim.save()
    result.raw["accounting"]["status"] = "updated"

    sim_payload = {"state": sim.state, "stats": sim.stats()}
    logging.getLogger(__name__).info("小G模拟盘: 净值 $%.0f，操作 %d 条",
                                     sim_out["equity"], len(sim_out["ops"]))

    # ---- S5 治理桥：内核 → 五元事件（治理旁路，失败只记 WARNING 不阻塞）----
    # 事件账 governance_events.jsonl 属会计账白名单（跨轮累计，不进决策输入）。
    from trading_system.governance_bridge import GovernanceBridge
    bridge = None
    try:
        bridge = GovernanceBridge(os.path.join(args.out, "governance_events.jsonl"))
    except Exception as exc:
        logging.getLogger(__name__).warning("治理桥初始化异常（不阻塞内核）: %s", exc)

    # ---- S6 复盘闭环：诸葛团队日度复盘（归因+违规标记+校准状态 → 复盘纪要）----
    # 复盘产物属会计账白名单，只进报告层与治理事件，不进决策输入（D5）。
    # review.daily 为延迟环节：pipeline 内记 deferred，此处实际执行后补账，
    # 补账先于 emit_from_result——治理事件中的 review.daily 状态才是真实的。
    review_path = None
    try:
        from trading_system.review.chief import ReviewChief
        chief = ReviewChief(out_dir=args.out, journal_path=j.path, bridge=bridge)
        review_out = chief.daily(result.trade_date)
        review_path = review_out["memo_path"]
        for step in result.raw.get("redline", []):
            if step["step"] == "review.daily":
                step.update(status="executed", ms=review_out["ms"], note="")
        logging.getLogger(__name__).info(
            "复盘纪要: %s（违规命中 %d 条）", review_path,
            review_out["attribution"]["total_violations"])
    except Exception as exc:
        # 注册表透传约定：纪要缺失→披露未生成原因（不阻塞内核）
        logging.getLogger(__name__).warning("复盘纪要未生成（披露原因）: %s", exc)
        for step in result.raw.get("redline", []):
            if step["step"] == "review.daily":
                step.update(status="passthrough", note=f"纪要未生成: {exc}")

    if bridge is not None:
        try:
            n_events = bridge.emit_from_result(result, sim_out)
            ok, errs = GovernanceBridge.verify_chain(bridge.path)
            logging.getLogger(__name__).info(
                "治理桥: 写入 %d 条五元事件，哈希链校验 %s", n_events,
                "通过" if ok else f"失败 {errs[:2]}")
        except Exception as exc:
            logging.getLogger(__name__).warning("治理桥异常（不阻塞内核）: %s", exc)

    # review.daily 补账（executed/未生成原因）在首次 to_json 之后——重写 JSON
    # 让落盘结果中的环节状态与治理事件一致（账实相符）。
    json_path = to_json(result, args.out)

    # ---- 盘前计划（premarket 模式）----
    plan_path = None
    if args.mode == "premarket":
        from trading_system.triggers import premarket_plan
        plan = premarket_plan(result)
        plan_path = os.path.join(args.out, f"盘前计划_{result.trade_date.replace('-', '')}.md")
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(plan)
        print(plan)

    # ---- 把胜率章节追加进日报 ----
    with open(md_path, "a", encoding="utf-8") as f:
        f.write("\n" + _journal_section(stats))

    # ---- HTML 报告（自包含单文件：结论/图形/卡片/红线轨迹/胜率）----
    html_path = None
    if args.html:
        from trading_system.report_html import render_html
        import json as _json
        entries = _json.load(open(j.path, encoding="utf-8")) if os.path.exists(j.path) else []
        # 策略验证中心基准线：QQQ + SPY 同期日线（取不到时页面优雅降级）
        bench: dict = {}
        for bmk in ("QQQ", "SPY"):
            try:
                bdf = _single_with_fallback(settle_provider, "ohlcv", bmk, days=400)
                if bdf is not None and len(bdf):
                    bench[bmk] = [[str(d.date()), round(float(c), 2)]
                                  for d, c in zip(bdf.index, bdf["Close"])]
            except Exception as exc:
                logging.getLogger(__name__).warning("基准 %s 日线获取失败（页面降级）: %s", bmk, exc)
        html_path = os.path.join(args.out, f"日报_{result.trade_date.replace('-', '')}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(result, stats, entries, sim=sim_payload, bench=bench,
                                asset_dir=args.out))

    print(f"\n报告已生成:\n  {md_path}\n  {json_path}" +
          (f"\n  {html_path}" if html_path else "") +
          (f"\n  {plan_path}" if plan_path else "") +
          (f"\n  {review_path}" if review_path else "") + "\n")
    if not args.quiet:
        print(render_markdown(result))


def _intraday(args, provider) -> None:
    from trading_system.providers import get_provider
    from trading_system.triggers import monitor_loop, watch_from_result

    files = sorted(glob.glob(os.path.join(args.out, "result_*.json")))
    if not files:
        raise SystemExit("未找到日报 result JSON，请先运行 daily 模式生成 picks。")
    latest = files[-1]
    # v6.1：监控对象必须是【新鲜】日报——拿着几天前的入场/止损参考价做盘中
    # 触发，等于用过期的地图开车（数据时效性红线）。
    import re as _re
    from datetime import datetime as _dt
    from trading_system.calendar import prev_trading_day as _ptd
    m = _re.search(r"result_(\d{4})-?(\d{2})-?(\d{2})", os.path.basename(latest))
    if m:
        rpt_date = _dt.strptime("".join(m.groups()), "%Y%m%d").date()
        earliest = _ptd(_dt.now().date())
        if rpt_date < earliest:
            raise SystemExit(
                f"最新日报为 {rpt_date}（最近交易日 {earliest}）——入场/止损参考价已过期，"
                "请先运行 daily 模式生成今日日报再监控（防止用陈旧数据触发交易动作）。")
    only = [t.strip().upper() for t in args.watch.split(",")] if args.watch else None
    watch = watch_from_result(latest, only)
    if not watch:
        raise SystemExit(f"{latest} 中无 picks 可监控。")
    print(f"监控 {len(watch)} 只（{os.path.basename(latest)}），"
          f"间隔 {args.interval}s × {args.cycles} 轮：")
    for w in watch:
        print(f"  {w['ticker']}: 入场 {w['entry']} / 止损 {w['stop']} / 保护 {w['protect']}")
    prov = get_provider(provider or ("demo" if args.demo else None))
    alerts = monitor_loop(watch, prov, interval_s=args.interval,
                          cycles=args.cycles,
                          on_alert=lambda a: print(f"  ⚡ {a['msg']}"))
    print(f"\n监控结束，共 {len(alerts)} 条触发。")


def _backtest(args, provider) -> None:
    from trading_system.backtest import (
        GateParams, collect_day_frames, run_backtest,
    )
    from trading_system.providers import get_provider
    from trading_system.universe import load_universe

    prov = get_provider(provider or ("demo" if args.demo else None))
    universe = load_universe(args.universe, args.universe_file)
    frames, panel, _ = collect_day_frames(
        prov, universe, days=args.bt_days + 200, signal_days=args.bt_days, top_n=args.top)
    res = run_backtest(frames, panel, GateParams())

    lines = [f"# 回测报告 — {res['n_days']} 个信号日（provider={prov.name}）", ""]
    lines.append(f"- 交易 **{res['n_trades']}** 笔 ｜ 胜率 **{res['win_rate']:.1%}** ｜ "
                 f"期望 **{res['expectancy_r']}R** ｜ 利润因子 {res['profit_factor']}")
    lines.append(f"- 累计 {res['total_r']}R ｜ 组合收益 {res['port_total_return']:.2%} ｜ "
                 f"夏普 {res['port_sharpe']} ｜ 最大回撤 {res['port_max_dd']:.2%}")
    lines.append("")
    # v6.0 诚实披露（白皮书§14 "在统计面前保持诚实"）：净口径与已知偏差
    lines.append("## 口径与已知偏差披露")
    lines.append(f"- 交易成本：单边 {config.COST_BPS}bp 已计入（净口径 R 与组合收益）")
    lines.append("- 幸存者偏差：回测使用【当前】股票池回放历史，已退市标的缺失，"
                 "结果方向性偏乐观；")
    lines.append("- 映射时点偏差：产业链归属/板块映射按【当前】定义应用于历史信号日；")
    lines.append("- 复权口径：yahoo/stooq/ifind 均为前复权（v6.0 统一）。")
    lines.append("")
    if res["by_template"]:
        lines.append("| 模板 | 笔数 | 胜率 | 平均R |")
        lines.append("|---|---|---|---|")
        for k, v in res["by_template"].items():
            lines.append(f"| {k} | {v['n']} | {v['win_rate']:.0%} | {v['avg_r']} |")
        lines.append("")
    path = os.path.join(args.out, f"回测_{prov.name}.md")
    os.makedirs(args.out, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\n回测报告: {path}")


def _tune(args, provider) -> None:
    from trading_system.backtest import (
        collect_day_frames, run_wfa, save_tuned_params,
    )
    from trading_system.providers import get_provider
    from trading_system.universe import load_universe

    prov = get_provider(provider or ("demo" if args.demo else None))
    universe = load_universe(args.universe, args.universe_file)
    frames, panel, _ = collect_day_frames(
        prov, universe, days=args.bt_days + 200, signal_days=args.bt_days, top_n=args.top)
    wfa = run_wfa(frames, panel)

    lines = ["# WFA 滚动前推调参报告", ""]
    lines.append(f"折数 {wfa.get('n_folds')} ｜ 网格 {wfa.get('grid_size')} 组合 ｜ "
                 f"试验校正 N={wfa.get('n_folds', 0) * wfa.get('grid_size', 0)}")
    oos = wfa.get("oos_aggregate", {})
    lines.append(f"\n## 样本外（OOS）汇总\n")
    lines.append(f"- 交易 {oos.get('trades')} 笔 ｜ 胜率 {oos.get('win_rate', 0):.1%} ｜ "
                 f"期望 {oos.get('expectancy_r')}R ｜ 夏普 {oos.get('sharpe')} ｜ "
                 f"回撤 {oos.get('max_dd', 0):.2%}")
    lines.append(f"- {wfa.get('dsr_note')}")
    lines.append(f"- 推荐: {wfa.get('recommended_note')}")
    lines.append("\n## 各折明细\n")
    lines.append("| 折 | 训练 | 测试 | 样本内参数 | IS SR | OOS胜率 | OOS期望R |")
    lines.append("|---|---|---|---|---|---|---|")
    for f_ in wfa.get("folds", []):
        lines.append(f"| {f_['fold']} | {f_['train']} | {f_['test']} | {f_['is_params']} "
                     f"| {f_['is_sharpe']:.2f} | {f_['oos_win_rate']:.0%} | {f_['oos_expectancy']} |")
    path = os.path.join(args.out, "WFA报告.md")
    os.makedirs(args.out, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    saved = save_tuned_params(wfa)
    print("\n".join(lines))
    print(f"\nWFA 报告: {path}" + (f"\n调优参数已写入: {saved}" if saved else ""))


def _review_admin(args) -> None:
    """S6 复盘审批流：--review-list / --review-approve / --review-reject。

    模拟盘阶段审批不阻塞 pipeline 运行（只影响参数提案生效）；
    对齐 WorkLoom 三手势：采纳（approve）/ 驳回（reject，原因必填）。
    """
    import os
    from trading_system.governance_bridge import GovernanceBridge
    from trading_system.review.chief import ReviewChief
    from trading_system.review.monthly import list_proposals

    proposals_dir = os.path.join(args.out, "review_proposals")
    bridge = GovernanceBridge(os.path.join(args.out, "governance_events.jsonl"))
    chief = ReviewChief(out_dir=args.out, proposals_dir=proposals_dir,
                        bridge=bridge)

    if args.review_list:
        props = list_proposals(proposals_dir)
        if not props:
            print("暂无参数提案（月度 WFA 生成；DSR 不显著的提案已自动 reject）。")
            return
        print(f"共 {len(props)} 个提案（{proposals_dir}）：")
        for p in props:
            line = (f"  {p.proposal_id}  [{p.status}]  DSR={p.dsr}  "
                    f"OOS期望={p.oos_expectancy}R  "
                    f"参数={p.grid_result.get('recommended_params')}")
            if p.status == "approved":
                line += f"  生效自 {p.effective_from}"
            if p.reason:
                line += f"\n      原因: {p.reason}"
            print(line)
        pending = [p for p in props if p.status == "pending_review"]
        if pending:
            print(f"\n待审批 {len(pending)} 个：--review-approve <id> 或 "
                  "--review-reject <id> --reason \"...\"")
        return

    if args.review_approve:
        p = chief.approve(args.review_approve,
                          tuned_path=os.path.join(args.out, "tuned_params.json"))
        print(f"提案 {p.proposal_id} 已批准：自 {p.effective_from} 起随 "
              f"--use-tuned 显式启用（默认仍不加载：零基线纪律）。\n"
              f"披露已写入 {os.path.join(args.out, 'tuned_params.json')} "
              f"并记入治理事件；次日运行的日报与复盘纪要将披露本次生效。")
        return

    if args.review_reject:
        if not args.reason or not args.reason.strip():
            raise SystemExit("错误：--review-reject 必须同时提供 "
                             "--reason \"...\"（驳回原因必填，三手势纪律）。")
        p = chief.reject(args.review_reject, args.reason)
        print(f"提案 {p.proposal_id} 已驳回，原因已记录并回流: {p.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 短线美股交易系统 v4.1")
    parser.add_argument("--mode", choices=["daily", "premarket", "intraday"], default="daily")
    parser.add_argument("--market", choices=["us", "cn", "hk"], default="us",
                        help="研究市场（默认 us；cn/hk 保留原币研究，未接汇率前暂停交易记账）")
    parser.add_argument("--provider",
                        choices=["yahoo", "stooq", "tencent", "sina", "eastmoney",
                                 "agentgw", "demo"],
                        default=None)
    parser.add_argument("--demo", action="store_true", help="离线演示模式（合成数据）")
    parser.add_argument("--universe", choices=["core", "extended", "full", "file"],
                        default="extended",
                        help="full=请求所选市场股票列表，实际范围以输出中的来源和预筛记录为准")
    parser.add_argument("--universe-file", default=None)
    parser.add_argument("--top", type=int, default=config.SCAN_TOP_N, help="扫描精评候选数")
    parser.add_argument("--picks", type=int, default=config.MAX_PICKS_DEFAULT, help="最终放行标的数上限")
    parser.add_argument("--account", type=float, default=100_000,
                        help="账户净值（仅 USD；不能用于 CNY/HKD 价格的仓位反推）")
    parser.add_argument("--out", default=config.REPORTS_DIR)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--backtest", action="store_true", help="运行无未来函数回测")
    parser.add_argument("--tune", action="store_true", help="滚动 WFA 调参 + DSR 校正")
    parser.add_argument("--bt-days", type=int, default=260, help="回测信号日数")
    parser.add_argument("--interval", type=int, default=300, help="盘中轮询间隔（秒）")
    parser.add_argument("--cycles", type=int, default=12, help="盘中轮询轮数")
    parser.add_argument("--watch", default=None, help="盘中监控子集（逗号分隔代码）")
    parser.add_argument("--use-tuned", action="store_true",
                        help="显式加载上一轮 WFA 调优参数（默认关闭：零基线纪律，每轮从零开始）")
    parser.add_argument("--html", action="store_true", default=True,
                        help="生成自包含 HTML 日报（默认开）")
    parser.add_argument("--no-html", dest="html", action="store_false",
                        help="关闭 HTML 日报")
    # S6 复盘审批流（三手势；模拟盘阶段不阻塞 pipeline，只影响提案生效）
    parser.add_argument("--review-list", action="store_true",
                        help="列出全部 WFA 参数提案及审批状态")
    parser.add_argument("--review-approve", default=None, metavar="ID",
                        help="批准提案：状态→approved，次日随 --use-tuned 生效并披露")
    parser.add_argument("--review-reject", default=None, metavar="ID",
                        help="驳回提案：必须同时提供 --reason（原因必填）")
    parser.add_argument("--reason", default=None,
                        help="驳回原因（--review-reject 必填，回流组织记忆）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    provider = "demo" if args.demo else args.provider

    if args.review_list or args.review_approve or args.review_reject:
        _review_admin(args)
        return

    if args.backtest or args.tune:
        if args.backtest:
            _backtest(args, provider)
        if args.tune:
            _tune(args, provider)  # 帧缓存复用，WFA 在缓存上跑
    elif args.mode == "intraday":
        _intraday(args, provider)
    else:
        _daily(args, provider)


if __name__ == "__main__":
    main()
