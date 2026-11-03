"""报告必须忠实展示输入证据，不能从缺失字段推导已发生的事实。

这些用例只检查渲染结果与原始台账、评分和扫描元数据之间的契约；
不调整评分、交易参数、成交/退出逻辑或收益台账。
"""

from __future__ import annotations

import copy
import html
import json
import re
from dataclasses import asdict
from xml.etree import ElementTree as ET

import pytest

from trading_system.agents.mrs_agent import MRSAgent
from trading_system.data_models import (
    DimensionScore, MRSResult, PipelineResult, StockCandidate, TradePick,
)
from trading_system.report import render_markdown, to_json
from trading_system.report_html import (
    _decision_card, _dim_reading, _equity_svg, _gauges_row_svg,
    _ledger_table, _lifecycle_card, _radar_svg, _result_from_json,
    _sim_tab, _stock_deep_annex, _verify_curve_svg, render_html,
)
from trading_system.review import weekly
from trading_system.simulator import SimEngine


def _text(value: str) -> str:
    value = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", value,
                   flags=re.S)
    return html.unescape(re.sub(r"<[^>]*>", "", value))


def _result(market="us", mode="extended", source="extended", picks=True):
    names = {"us": "美股", "cn": "A股", "hk": "港股"}
    dims = {name: DimensionScore(name, score)
            for name, score in (("macro", 8.0), ("flow", 7.0), ("sent", 7.0),
                                ("tech", 8.0), ("micro", None))}
    candidate = StockCandidate(
        ticker="AAPL", rank_score=6.4, price=101.0, adv_usd=150_000_000,
        atr_pct=0.03, s_structure=8.0, s_momentum=7.0, s_options=None,
        tss_final=7.5, entry_template="A", key_level=100.0,
    )
    pick = TradePick("AAPL", 7.5, 3.2, "A", 100.0, 95.0, 10, 0.02, 50.0)
    raw = {
        "market": {
            "market_id": market, "short_name": names[market],
            "name": names[market], "timezone": "fixture-zone",
            "settlement": "fixture", "limit_note": "fixture",
            "benchmarks": {"index": "BENCH", "index_label": "本市场基准"},
            "scan_filters": {"min_price": 1, "min_adv": 1000, "currency": "fixture"},
        },
        "universe_mode": mode, "universe_source": source,
        "scan_stats": {"universe_size": 480, "passed": 100, "selected": 8,
                       "rejected": {"nodata": 380}, "top10": [("AAPL", 6.4)]},
        "freshness": {"benchmark_last_bar": "2026-10-19", "benchmark_lag_days": 0},
        "source_lineage": [("ohlcv:AAPL", "yahoo"), ("ohlcv:BENCH", "tencent")],
        "pick_rationale": {"AAPL": {"mode": "标准做多", "standard": True,
                                      "shares": 10, "entry": 100.0, "stop": 95.0,
                                      "r_usd": 50.0, "position_pct": 0.02}},
    }
    return PipelineResult(
        "2026-10-20", "yahoo",
        mrs=MRSResult(7.5, 1.0, 1.0, 7.5, dimensions=dims,
                      regime="fixture", position_cap=(0.4, 0.7)),
        watchlist=[candidate], picks=[pick] if picks else [],
        action="BUY" if picks else "HOLD", market_view="本轮结果说明", raw=raw,
    )


def _sim(tmp_path, initial=25_000.0, positions=False, pending=False):
    engine = SimEngine(str(tmp_path / "sim.json"), initial_cash=initial)
    engine.state["equity_curve"] = [
        {"date": "2026-10-16", "equity": initial},
        {"date": "2026-10-19", "equity": initial * 1.02},
    ]
    if positions:
        engine.state["positions"] = [{"ticker": "AAA", "shares": 10,
                                        "entry_price": 100.0, "stop": 95.0,
                                        "risk_usd": 50.0, "entry_date": "2026-10-16"}]
    if pending:
        engine.state["pending"] = [{"ticker": "BBB"}]
    return {"state": engine.state, "stats": engine.stats()}


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), True, "5"])
def test_radar_missing_score_has_no_point_or_filled_polygon(missing):
    svg = _radar_svg({"macro": {"score": 8.0}, "flow": {"score": missing},
                      "tech": {"score": 0.0}})
    root = ET.fromstring(svg)
    assert len(root.findall("circle")) == 2, "缺失维度不能补成5分或0分数据点"
    assert all(p.get("fill") == "none" for p in root.findall("polygon"))
    assert "缺失" in "".join(root.itertext())
    assert "nan" not in svg and "inf" not in svg


def test_radar_complete_scores_keep_real_zero_and_polygon():
    root = ET.fromstring(_radar_svg({"macro": {"score": 8.0},
                                    "flow": {"score": 5.0}, "tech": {"score": 0.0}}))
    assert len(root.findall("circle")) == 3
    assert sum(p.get("fill") != "none" for p in root.findall("polygon")) == 1
    assert "0.0" in "".join(root.itertext())


def test_missing_dimension_and_options_describe_exclusion():
    r = _result()
    for value in (_dim_reading("micro", None), _decision_card(r, r.picks[0]),
                  _stock_deep_annex(r, r.picks[0])):
        assert "剔除" in value and "归一化" in value
        assert "按中性处理" not in value
    page = _text(render_html(r))
    assert "可用维度协调度" in page and "缺失维度 1" in page
    assert "五个维度得分接近" not in page and "五维协调度" not in page


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), True])
def test_gauge_missing_score_is_unknown(missing):
    svg = _gauges_row_svg({"micro": {"score": missing}})
    assert "nan" not in svg and "inf" not in svg
    assert "数据缺失" in _text(svg)


def test_mrs_evidence_matches_unchanged_renormalized_score(monkeypatch):
    agent = MRSAgent(None)
    scores = {"_macro": ("macro", 8.0), "_flow": ("flow", 8.0),
              "_sentiment": ("sent", 8.0), "_technical": ("tech", 8.0),
              "_micro": ("micro", None)}
    for method, (name, score) in scores.items():
        monkeypatch.setattr(agent, method,
                            lambda *a, n=name, s=score: DimensionScore(
                                n, s, missing=["未提供"] if s is None else []))
    monkeypatch.setattr(agent, "_detect_shock", lambda *a: (False, ""))
    monkeypatch.setattr(agent, "_liquidity_discount", lambda *a: (1.0, ""))
    result = agent.execute({"market_data": {"spy": None, "universe_closes": {}}})
    assert result.mrs_raw == result.mrs_star == 8.0 and result.delta == 0.0
    assert "中性5" not in " ".join(result.evidence)
    assert any("[缺失→剔除再归一化]" in line for line in result.evidence)


@pytest.mark.parametrize("provider", ["yahoo", "stooq", "tiingo"])
def test_daily_data_has_date_and_no_realtime_claim(provider):
    r = _result()
    r.provider = provider
    page = _text(render_html(r))
    assert "实时行情" not in page
    assert "基准最新日K" in page and "2026-10-19" in page
    assert "本市场基准" in page and "个股时点以各自行情为准" in page
    assert "四环降级" not in page
    assert "日K收盘价" in page


def test_absent_freshness_does_not_invent_current_asof():
    r = _result()
    r.raw.pop("freshness")
    for value in (render_html(r), render_markdown(r)):
        assert "基准最新日K日期未知" in _text(value)
        assert "2026-10-19" not in value


@pytest.mark.parametrize("market,label", [("us", "美股"), ("cn", "A股"), ("hk", "港股")])
def test_market_label_comes_from_result(market, label):
    r = _result(market=market)
    value = render_html(r)
    page = _text(value)
    assert f"本报告市场：{label}" in page
    assert f"{label}个股深度报告" in page
    assert f"投资标的：{label}" in page
    if market != "us":
        assert "US STOCK" not in value
        assert "能力的美股交易系统" not in page
        assert "跑赢标普 500" not in page
    assert f"短线{label}交易日报" in render_markdown(r)


def test_missing_market_metadata_stays_unknown():
    r = _result()
    r.raw.pop("market")
    for value in (render_html(r), render_markdown(r)):
        assert "市场未知" in _text(value)
    assert "本报告市场：美股" not in _text(render_html(r))


@pytest.mark.parametrize("currency", ["USD", "CNY", "HKD", None, "<currency>"])
def test_quote_currency_comes_from_metadata_without_changing_amounts(currency):
    r = _result(market="cn")
    r.raw["market"]["currency"] = currency
    before = copy.deepcopy(asdict(r))
    label = currency or "币种未知"
    value = render_html(r)
    assert f"{label} 101.00" in _text(value)
    assert f"{label} 150M" in _text(_stock_deep_annex(r, r.picks[0]))
    assert "$101.00" not in value and "$150M" not in value
    if currency == "<currency>":
        assert "<currency>" not in value and "&lt;currency&gt;" in value
    assert asdict(r) == before


@pytest.mark.parametrize("mode,source,expected", [
    ("core", "core", "内置核心池"), ("extended", "extended", "内置扩展池"),
    ("file", "file", "自定义文件"), ("full", "nasdaqtrader", "Nasdaq Trader"),
    ("full", "eastmoney", "东方财富"), ("full", "cache", "缓存列表"),
    ("full", "fallback", "降级备选列表"), ("extended", "embedded", "内置列表"),
])
def test_scan_scope_uses_actual_mode_and_source(mode, source, expected):
    r = _result(mode=mode, source=source)
    r.raw["prefilter"] = "预筛 6123→5900→流动性通过 850→重量级拉取 480"
    for value in (render_html(r), render_markdown(r)):
        page = _text(value)
        assert expected in page and r.raw["prefilter"] in page
        assert "本轮扫描池" in page
        assert "一个都不漏" not in page and "官方清单全量" not in page
        assert "全市场 6000+" not in page and "全市场质量前十" not in page
    assert "扫描初排前十" in _text(render_html(r))


def test_missing_scope_source_is_disclosed_and_metadata_escaped():
    r = _result()
    r.raw.pop("universe_source")
    r.raw["prefilter"] = '<script>alert("scope")</script>'
    r.raw["market"]["short_name"] = '<img onerror="attack">'
    value = render_html(r)
    assert "清单来源：未知" in value
    assert "<script>alert" not in value and '<img onerror="attack">' not in value
    assert "&lt;script&gt;" in value and "&lt;img" in value


def test_zero_scan_pool_stays_zero():
    r = _result(picks=False)
    r.raw["scan_stats"] = {"universe_size": 0, "passed": 0, "selected": 0}
    page = _text(render_html(r))
    assert "① 本轮扫描池0 只" in page
    assert "① 本轮扫描池1 只" not in page


def test_sim_uses_persisted_initial_cash_everywhere(tmp_path):
    sim = _sim(tmp_path)
    before = copy.deepcopy(sim)
    page = _text(_sim_tab(sim))
    assert "初始资金 $25,000" in page and "初始 $25,000" in page
    assert "$100,000" not in page and "+2.0%" in page
    assert sim == before


def test_unknown_initial_cash_does_not_fabricate_baseline_or_return(tmp_path):
    sim = _sim(tmp_path)
    sim["state"].pop("initial_cash")
    page = _text(_sim_tab(sim))
    assert "初始资金未知" in page and "累计收益未知" in page
    assert "初始 $" not in page and "$100,000" not in page
    assert "$25,500" in page
    assert "初始 $" not in _text(_equity_svg(sim["state"]["equity_curve"], None))


def test_no_sim_payload_does_not_invent_a_ledger():
    page = _text(render_html(_result()))
    assert "未提供模拟盘台账" in page
    assert "初始资金 $100,000" not in page and "以 10 万美元" not in page


@pytest.mark.parametrize("positions,pending,expected", [
    (False, False, "当前无持仓，也无待成交信号"),
    (True, False, "已有持仓，尚无已结算交易"),
    (False, True, "待成交信号 1 只"),
    (True, True, "待成交信号 1 只"),
])
def test_sim_empty_history_describes_actual_positions_and_queue(tmp_path, positions, pending, expected):
    page = _text(_sim_tab(_sim(tmp_path, positions=positions, pending=pending)))
    assert expected in page
    if not pending:
        assert "第一笔信号正在等待" not in page


def test_no_new_picks_does_not_claim_existing_holdings_are_cash(tmp_path):
    sim = _sim(tmp_path, positions=True)
    page = _text(render_html(_result(picks=False), sim=sim))
    assert "今日无新放行标的" in page
    assert "今日未开仓" not in page and "今日纪律：空仓等待" not in page


def test_risk_is_plan_not_guaranteed_loss_cap(tmp_path):
    r = _result()
    page = _text(render_html(r, sim=_sim(tmp_path)))
    for forbidden in ("单笔最大亏损", "亏损锁定", "从此不亏钱", "最坏情况亏多少，是算得出来",
                      "最多失血净值", "亏钱的交易不回头", "单票永远不至于毁灭账户"):
        assert forbidden not in page
    assert "计划风险" in page and "$50" in page
    assert "跳空、滑点与佣金可能使实际亏损超过计划风险" in page
    assert "不保证保本" in page


@pytest.mark.parametrize("limit", [0.1, 0.35])
def test_single_position_explanation_uses_current_config(tmp_path, monkeypatch, limit):
    monkeypatch.setattr("trading_system.config.MAX_SINGLE_POSITION_PCT", limit)
    page = _text(_sim_tab(_sim(tmp_path)))
    assert f"不超过净值 {limit:.0%}" in page
    assert "不超过净值两成" not in page


def test_pending_void_and_open_positions_have_distinct_display():
    records = [
        {"ticker": "PENDING", "status": "open", "entry": None},
        {"ticker": "HELD", "status": "open", "entry": 100.0},
        {"ticker": "VOID", "status": "void", "entry": None, "note": "跳空作废"},
    ]
    pending, held, void = [_text(_lifecycle_card(e)) for e in records]
    assert "待成交" in pending and "持仓中" not in pending
    assert "持仓中" in held and "待成交" not in held
    assert "已作废" in void and "待成交" not in void and "持仓中" not in void
    ledger = _text(_ledger_table(records))
    assert "已作废" in ledger
    page = _text(render_html(_result(picks=False), journal_entries=records))
    assert "持仓中 1 笔，待成交 1 笔" in page
    assert "持仓中 2 笔" not in page


def test_execution_trace_preserves_non_ai_states():
    r = _result()
    r.raw["redline"] = [
        {"step": "data.prepare", "status": "executed", "ms": 1},
        {"step": "review.daily", "status": "deferred", "ms": 0},
        {"step": "custom.failure", "status": "failed", "ms": 3},
        {"step": "custom.skip", "status": "skipped", "ms": 0},
        {"step": "clean.llm_semantic", "status": "passthrough", "ms": 2},
    ]
    page = _text(render_html(r))
    for state in ("已执行", "待编排", "失败", "已跳过", "透传"):
        assert state in page
    assert "4 个 AI 语义环节本轮不可用" not in page


def test_verification_curve_requires_dated_marks_not_closed_trades(tmp_path):
    sim = _sim(tmp_path)
    svg = _verify_curve_svg(sim, None)
    assert "<polyline" in svg
    sim["state"]["equity_curve"] = sim["state"]["equity_curve"][:1]
    page = _text(_verify_curve_svg(sim, None))
    assert "至少两个不同日期的净值记录" in page
    assert "信号成交结算后" not in page


def test_verification_curve_uses_common_started_window_and_actual_end_dates():
    sim = {"state": {"initial_cash": 25_000, "equity_curve": [
        {"date": "2026-10-15", "equity": 25_000},
        {"date": "2026-10-16", "equity": 26_000},
        {"date": "2026-10-19", "equity": 27_000},
    ]}}
    bench = {"QQQ": [["2026-10-16", 100], ["2026-10-20", 110]],
             "SPY": [["2026-10-15", 100], ["2026-10-19", 105]]}
    before = copy.deepcopy((sim, bench))
    page = _text(_verify_curve_svg(sim, bench))
    assert "共同窗口 2026-10-16 至 2026-10-19" in page
    assert "2026-10-20" not in page
    assert "QQQ 最后真实记录 2026-10-16" in page
    assert "小虎模拟盘 最后真实记录 2026-10-19" in page
    assert "+3.8%" in page and "+0.0%" in page and "+5.0%" in page
    assert (sim, bench) == before


def test_verification_curve_nonoverlap_discloses_no_comparable_window():
    sim = {"state": {"equity_curve": [
        {"date": "2026-10-15", "equity": 25_000},
        {"date": "2026-10-16", "equity": 26_000},
    ]}}
    page = _text(_verify_curve_svg(sim, {"QQQ": [["2026-10-19", 100], ["2026-10-20", 101]]}))
    assert "无共同日期窗口" in page


def test_weekly_bucket_labels_match_exact_unchanged_boundaries():
    values = [-1.01, -1.0, -0.99, 0.0, 0.01, 1.0, 1.01, 2.0, 2.01]
    assert weekly.r_distribution(values) == {
        "≤-1R": 2, "-1~0R": 2, "0~1R": 2, "1~2R": 2, ">2R": 1,
    }
    assert "左开右闭" in weekly.r_distribution.__doc__


def test_markdown_appendix_calls_open_records_unsettled():
    import main
    stats = {"closed": 1, "open": 2, "total_r": 1.0, "win_rate": 1.0,
             "expectancy_r": 1.0, "profit_factor": 2.0, "last20": {"n": 0},
             "last50": {"n": 0}}
    value = main._journal_section(stats)
    assert "未结算信号 2" in value and "持仓中 2" not in value


def test_review_memo_calls_open_records_unsettled(tmp_path):
    from trading_system.review.chief import ReviewChief
    from trading_system.review.attribution import attribute_journal

    records = [{"date": "2026-10-20", "ticker": "AAA", "status": "open",
                "entry": None, "entry_ref": 100.0, "stop": 95.0}]
    chief = ReviewChief(out_dir=str(tmp_path))
    value = chief._render_daily("2026-10-20", records, attribute_journal(records), {}, None, [])
    assert "未结算信号 1" in value and "持仓中 1" not in value
    assert "待成交" in value and "持仓中" not in value


def test_attribution_labels_entry_state_without_changing_violations():
    from trading_system.review.attribution import attribute_record
    rec = {"status": "open", "entry": None, "entry_ref": 100.0, "mrs_star": 3.0}
    pending = attribute_record(rec)
    assert pending["exit_type"] == "待成交" and pending["violations"] == []
    rec["entry"] = 100.0
    held = attribute_record(rec)
    assert held["exit_type"] == "持仓中" and "V1" in held["violations"]
    rec.update(status="void", entry=None)
    void = attribute_record(rec)
    assert void["exit_type"] == "作废" and void["violations"] == []


def test_atr_fraction_is_rendered_as_percentage_without_rescaling_source():
    r = _result()
    page = _text(_decision_card(r, r.picks[0]))
    assert "日均波动 3.0%" in page and "日均波动 0.0%" not in page
    assert r.watchlist[0].atr_pct == 0.03


def test_main_help_does_not_promise_us_full_universe(monkeypatch, capsys):
    import main
    monkeypatch.setattr("sys.argv", ["main.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 0
    value = capsys.readouterr().out
    assert "6000+" not in value and "纽交所+纳斯达克" not in value
    assert "所选市场" in value and "实际范围" in value


def test_json_roundtrip_preserves_report_facts_without_mutation(tmp_path):
    r = _result(market="hk", mode="full", source="fallback")
    before = copy.deepcopy(asdict(r))
    restored = _result_from_json(to_json(r, str(tmp_path)))
    page = _text(render_html(restored))
    assert "本报告市场：港股" in page and "降级备选列表" in page
    assert "缺失维度 1" in page and "2026-10-19" in page
    assert asdict(r) == before
    assert asdict(restored)["raw"] == json.loads(json.dumps(before["raw"]))


def test_required_model_watch_is_complete_in_both_daily_reports():
    r = _result(picks=False)
    r.action = "WAIT"
    r.market_view = "等待量价共同确认。"
    r.raw.update(
        model={"required": True, "all_required_completed": True, "model": "deepseek-flash",
               "call_count": 6, "required_stages": ["a", "b", "c", "d", "e"], "search_count": 14},
        execution_decisions=[{
            "symbol": "AAPL", "action": "watch", "target_weight": 0,
            "action_summary": "纸面观察苹果。", "why_this": "收入趋势有待交叉核实。",
            "why_now": "观察日接近前期区间上沿。",
            "entry_condition": "等待收盘站稳后复评。", "invalidation": "跌回区间中部则取消。",
            "risk_note": "业绩消息尚未得到独立确认。",
            "market_evidence": [{"symbol": "AAPL", "as_of": "2026-10-19", "close": 101.25}],
            "sources": [{"id": "ds_1", "title": "AAPL & sector",
                         "url": "https://example.com/company/filing", "published_at": None}],
            "entry_price": None, "stop_price": None, "shares": None, "risk_usd": None,
            "time_stop_days": None, "valid_for": "2026-10-21T16:00:00-04:00",
            "reference_capital_usd": 100000,
        }])
    before = copy.deepcopy(asdict(r))
    for report in (render_markdown(r), render_html(r)):
        visible = _text(report)
        for value in ("模型最终判断与执行条件", r.market_view, "纸面观察苹果。",
                      "收入趋势有待交叉核实。", "观察日接近前期区间上沿。",
                      "等待收盘站稳后复评。", "跌回区间中部则取消。",
                      "业绩消息尚未得到独立确认。", "2026-10-19", "101.25",
                      "2026-10-21T16:00:00-04:00", "AAPL & sector", "未提供下单价格或股数"):
            assert value in visible
        assert 'href="https://example.com/company/filing"' in report
    assert asdict(r) == before


def test_required_model_with_no_single_stock_instructions_still_reports_the_final_decision():
    r = _result(picks=False)
    r.action, r.market_view = "AVOID", "行业与个股证据不足，回避新增纸面信号。"
    r.raw.update(
        model={"required": True, "all_required_completed": True, "model": "deepseek-flash",
               "call_count": 6, "required_stages": ["a", "b", "c", "d", "e"], "search_count": 14},
        execution_decisions=[])
    for report in (render_markdown(r), render_html(r)):
        assert "本轮没有新增单票指令" in report
        assert r.market_view in report


@pytest.mark.parametrize(("candidates", "expected"), [
    ({}, ["本轮没有标的通过数值风控，未启动按标的检索"]),
    ({"AAPL": {"status": "filing_verified", "source_ids": ["ds_1"]}},
     ["1 只标的通过数值风控，1 只取得合格的 SEC 公司文件"]),
    ({"AAA": {"status": "filing_verified", "source_ids": ["ds_1"]},
      "BBB": {"status": "no_results", "source_ids": []},
      "CCC": {"status": "no_eligible_filing", "source_ids": []},
      "DDD": {"status": "search_unavailable", "source_ids": []}},
     ["4 只标的通过数值风控，1 只取得合格的 SEC 公司文件",
      "检索零命中 1 只", "未取得合格文件 1 只", "检索不可用 1 只", "本轮仅可观察"]),
    ({"AAA": {"status": ["<img src=x onerror=alert(1)>"]}},
     ["公司证据检查记录不完整"]),
])
def test_required_model_reports_numeric_and_company_evidence_outcomes(candidates, expected):
    from trading_system.llm.company_evidence import COMPANY_EVIDENCE_CONTRACT

    r = _result(picks=False)
    r.action = "WAIT"
    r.raw.update(
        model={"required": True, "all_required_completed": True, "model": "deepseek-flash",
               "call_count": 5, "required_stages": [], "search_count": 14},
        execution_decisions=[],
        debate={"company_evidence": {"contract": COMPANY_EVIDENCE_CONTRACT, "candidates": candidates}},
    )
    before = copy.deepcopy(asdict(r))
    markdown = render_markdown(r)
    page = render_html(r)
    for report in (markdown, page):
        visible = _text(report)
        assert all(fragment in visible for fragment in expected)
        assert "<img src=x onerror=alert(1)>" not in report
    assert "无达标标的，等待" not in markdown
    assert asdict(r) == before

    # Older reports have no SEC candidate outcome contract.
    r.raw["debate"].pop("company_evidence")
    assert "公司证据检查" not in render_markdown(r)
    assert "公司证据检查" not in render_html(r)
