"""报告生成 — Markdown 日报（四行看板 + 产业链周期章节 + 交易卡片）。"""

from __future__ import annotations

import json
from dataclasses import asdict, fields, is_dataclass
from html import escape
from pathlib import Path
import re

from . import config
from .data_models import PipelineResult


def _market_name(raw: dict) -> str:
    """报告市场只能由本轮元数据确定；旧报告缺字段时保留未知。"""
    market = raw.get("market") or {}
    return str(market.get("short_name") or
               {"us": "美股", "cn": "A股", "hk": "港股"}.get(market.get("market_id"))
               or "市场未知")


def _market_currency(raw: dict) -> str:
    """只展示输入声明的行情币种；不由市场名推断或进行换算。"""
    return str((raw.get("market") or {}).get("currency") or "币种未知")


def _execution_blocked(raw: dict) -> bool:
    return any((raw.get(key) or {}).get("execution_blocked")
               for key in ("accounting", "currency_policy"))


def _accounting_note(raw: dict) -> str:
    for key in ("accounting", "currency_policy"):
        policy = raw.get(key) or {}
        if policy.get("execution_blocked"):
            return str(policy.get("reason") or "USD 账户币种边界受阻，交易记账已暂停。")
    if raw.get("account_currency") or raw.get("currency_policy"):
        return "账户净值、风险预算与交易账本以 USD 计；原币报价未经汇率换算不得入账。"
    return ""


def _universe_note(raw: dict) -> str:
    """请求模式、实际列表来源与扫描数量是三个不同的事实。"""
    modes = {"core": "内置核心池", "extended": "内置扩展池",
             "full": "请求全量清单", "file": "自定义文件"}
    sources = {"core": "内置核心池", "extended": "内置扩展池",
               "embedded": "内置列表", "file": "自定义文件",
               "nasdaqtrader": "Nasdaq Trader 官方列表", "eastmoney": "东方财富列表",
               "cache": "缓存列表", "fallback": "降级备选列表"}
    mode = raw.get("universe_mode")
    source = raw.get("universe_source")
    note = (f"股票池模式：{modes.get(mode, mode or '未知')}；"
            f"清单来源：{sources.get(source, source or '未知')}；"
            "数量表示本轮扫描池，不代表上市股票全量覆盖")
    if raw.get("prefilter"):
        note += f"；预筛记录：{raw['prefilter']}"
    return note


def _freshness_note(raw: dict) -> str:
    """仅展示已提供的基准日K日期，不把它推广为全部股票的时点。"""
    freshness = raw.get("freshness") or {}
    benchmarks = (raw.get("market") or {}).get("benchmarks") or {}
    label = benchmarks.get("index_label") or benchmarks.get("index") or "基准"
    latest = freshness.get("benchmark_last_bar")
    session = freshness.get("market_close")
    if session:
        macro = session["macro_last_bars"]
        return (f"已完成收盘交易日 {session['session']}（纽约），基准、已覆盖板块 ETF 和个股均按该日收盘对齐。"
                f"缺该日可用收盘的板块 ETF {len(session['etf_missing'])} 只、个股 {len(session['stock_missing'])} 只。"
                f"利率基准末根 {macro['tnx'] or '缺失'}，波动率基准末根 {macro['vix'] or '缺失'}，"
                f"短期波动率末根 {macro['vix9d'] or '缺失'}。净值图的 SPY 来自同一基准序列，本轮未采集 QQQ 对照。"
                "行情使用供应商报告的日线，可能延迟或修订。")
    note = (f"基准最新日K：{label} {latest}" if latest else "基准最新日K日期未知")
    lag = freshness.get("benchmark_lag_days")
    if lag is not None:
        note += f"；相对预期交易日滞后 {lag} 个自然日"
    return note + "；个股时点以各自行情为准"



def project_report(value):
    """Explicit JSON projection, preserving strategy objects and disclosing missingness."""
    import math
    from datetime import date, datetime
    import numpy as np
    missing = []
    active = set()

    def visit(item, path):
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if math.isfinite(item):
                return item
            missing.append({"path": path, "reason": "nan" if math.isnan(item)
                            else "positive_infinity" if item > 0 else "negative_infinity"})
            return None
        if isinstance(item, np.generic):
            return visit(item.item(), path)
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        identity = id(item)
        if identity in active:
            raise TypeError("Cyclic report value")
        active.add(identity)
        try:
            if is_dataclass(item) and not isinstance(item, type):
                return {field.name: visit(getattr(item, field.name), path + "/" + field.name)
                        for field in fields(item)}
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    raise TypeError("Report mapping keys must be strings")
                return {key: visit(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
                        for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [visit(child, path + "/" + str(index)) for index, child in enumerate(item)]
            raise TypeError(f"Unsupported report value: {type(item).__name__}")
        finally:
            active.remove(identity)

    return visit(value, ""), missing

def to_json(result: PipelineResult, out_dir: str = config.REPORTS_DIR, *, strict: bool = False) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts = result.trade_date.replace("-", "")
    path = Path(out_dir) / f"result_{ts}.json"
    if strict:
        from .ledger_io import write_json_atomic
        value, missing = project_report(result)
        value.setdefault("raw", {})["serialization"] = {
            "schema": "tiger.report-json.v1", "nonfinite_values": missing,
        }
        write_json_atomic(path, value)
    else:
        path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    return str(path)


def to_markdown(result: PipelineResult, out_dir: str = config.REPORTS_DIR, *, strict: bool = False) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts = result.trade_date.replace("-", "")
    path = Path(out_dir) / f"日报_{ts}.md"
    if strict:
        from .ledger_io import write_text_atomic
        write_text_atomic(path, render_markdown(result))
    else:
        path.write_text(render_markdown(result), encoding="utf-8")
    return str(path)


def _debate_card_section(ticker: str, debate: dict) -> str:
    """交易卡片"第六段：多空辩论证据"（v6.3 S2）。

    三种状态：有证据 → 渲染证据；触发但 LLM 透传 → 标注"本轮无辩论证据"；
    未触发（灰区外）→ 空串（不渲染）。
    """
    ev = (debate.get("evidence") or {}).get(ticker)
    if ev:
        bull = "；".join(ev.get("bull_points") or []) or "—"
        bear = "；".join(ev.get("bear_points") or []) or "—"
        fals = "；".join(ev.get("falsification_conditions") or []) or "—"
        return (
            f"  多空辩论证据（第六段，仅供复核，不影响评分与闸门）:\n"
            f"    多头论据: {bull}\n"
            f"    空头论据: {bear}\n"
            f"    裁决: {ev.get('coordinator_verdict', '存疑')}"
            f"（{ev.get('verdict_reason', '')}）\n"
            f"    证伪条件: {fals}"
        )
    if ticker in (debate.get("triggered") or []):
        if debate.get("status") == "passthrough":
            return "  多空辩论证据（第六段）: 本轮无辩论证据（LLM 不可用，红线透传兜底）"
        return "  多空辩论证据（第六段）: 本轮无有效辩论产出（已如实记录）"
    return ""


def model_instructions(raw: dict) -> list[dict] | None:
    """Distinguish an assessed empty decision set from a legacy report."""
    model = raw.get("model") or {}
    if model.get("required") is True and model.get("all_required_completed") is True:
        return raw["execution_decisions"]
    return None


def company_evidence_note(raw: dict) -> str:
    """Explain the recorded SEC lookup outcomes without repeating model prose."""
    debate = raw.get("debate")
    company = debate.get("company_evidence") if isinstance(debate, dict) else None
    if not isinstance(company, dict):
        return ""
    from .llm.company_evidence import COMPANY_EVIDENCE_CONTRACT

    if company.get("contract") != COMPANY_EVIDENCE_CONTRACT:
        return ""
    candidates = company.get("candidates")
    unavailable = "公司证据检查记录不完整。"
    if not isinstance(candidates, dict):
        return unavailable
    if not candidates:
        return "公司证据检查显示，本轮没有标的通过数值风控，未启动按标的检索。"

    labels = {"no_results": "检索零命中", "no_eligible_filing": "未取得合格文件",
              "search_unavailable": "检索不可用"}
    counts = dict.fromkeys(("filing_verified", *labels), 0)
    for outcome in candidates.values():
        status = outcome.get("status") if isinstance(outcome, dict) else None
        if not isinstance(status, str) or status not in counts:
            return unavailable
        counts[status] += 1
    eligible = counts["filing_verified"]
    note = (f"公司证据检查显示，{len(candidates)} 只标的通过数值风控，"
            f"{eligible} 只取得合格的 SEC 公司文件。")
    if eligible < len(candidates):
        gaps = "、".join(f"{label} {counts[status]} 只"
                        for status, label in labels.items() if counts[status])
        note += f"其余标的中，{gaps}。缺合格文件的标的本轮仅可观察。"
    return note


def model_instruction_fields(item: dict) -> list[tuple[str, str]]:
    """Describe a verified plan without computing new execution values."""
    market = item["market_evidence"][0]
    plan = "仅观察，未提供下单价格或股数。"
    if item["action"] == "buy":
        plan = (f"入场参考 USD {item['entry_price']:,.2f} · 止损 USD {item['stop_price']:,.2f} · "
                f"{item['shares']} 股 · 目标仓位 {item['target_weight']:.1%} · "
                f"计划风险 USD {item['risk_usd']:,.2f} · "
                f"资金基准 USD {item['reference_capital_usd']:,.2f}")
        if item["time_stop_days"] is not None:
            plan += f" · 时间止损 {item['time_stop_days']} 个交易日"
    return [
        ("为什么关注", item["why_this"]), ("为什么现在", item["why_now"]),
        ("行情依据", f"{market['symbol']} · {market['as_of']} 日线收盘 USD {market['close']:,.2f}"),
        ("纸面计划", plan), ("入场条件", item["entry_condition"]),
        ("失效条件", item["invalidation"]), ("主要风险", item["risk_note"]),
        ("有效至（纽约时间）", item["valid_for"]),
    ]


def _markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+!|~])", r"\\\1", escape(value))


def model_source_html(source: dict, *, markdown: bool = False) -> str:
    """Render a verified public citation without activating an unsafe URL."""
    from .llm.deepseek_required import sanitize_url
    format_text = _markdown_text if markdown else escape
    title = format_text(source["title"])
    url = source["url"]
    published = format_text(source["published_at"] or "发布时间未知")
    if sanitize_url(url) == url:
        title = f'<a href="{escape(url)}" rel="noreferrer">{title}</a>'
    return f"{title}（{published}）"


def render_markdown(r: PipelineResult) -> str:
    m = r.mrs
    mk = r.raw.get("market") or {}
    mk_grad = r.raw.get("market_grad") or {}
    instructions = model_instructions(r.raw)
    company_note = company_evidence_note(r.raw) if instructions is not None else ""
    lines: list[str] = []
    lines.append(f"# AI 短线{_market_name(r.raw)}交易日报 — {r.trade_date}")
    lines.append("")
    lines.append(f"> 数据源: {r.provider} ｜ 股票池: {r.raw.get('universe_mode')} "
                 f"（覆盖 {r.raw.get('data_coverage')}）｜ 耗时 {r.raw.get('elapsed_s')}s")
    lines.append(f"> {_universe_note(r.raw)}")
    lines.append(f"> {_freshness_note(r.raw)}")
    lines.append(f"> 行情报价币种：{_market_currency(r.raw)}")
    if _accounting_note(r.raw):
        lines.append(f"> {_accounting_note(r.raw)}")
    if mk:
        grad_txt = ("标准通道已开放" if mk_grad.get("graduated", True)
                    else f"新市场验证期（{mk_grad.get('settled', 0)}"
                         f"/{mk_grad.get('required', 50)}）·强制轻仓")
        if _execution_blocked(r.raw):
            grad_txt = ("毕业条件已满足" if mk_grad.get("graduated", True) else
                        f"新市场验证期（{mk_grad.get('settled', 0)}"
                        f"/{mk_grad.get('required', 50)}）·轻仓门槛保留") + "；交易记账已暂停"
        lines.append(f"> 市场: **{mk.get('name')}**（{mk.get('market_id')} ｜ "
                     f"{mk.get('timezone')} ｜ 结算 {mk.get('settlement')} ｜ "
                     f"{mk.get('limit_note')}）｜ 通道状态: {grad_txt}")
    lines.append("")

    # ---- 老板版四行看板（白皮书 §2.7）----
    lines.append("## 一、四行看板")
    lines.append("")
    if m:
        main_sectors = [s for s in r.sectors if s.in_main_pool]
        sub_sectors = [s for s in r.sectors if s.in_sub_pool]
        main_txt = "；".join(f"{s.etf}(SHS {s.shs})" for s in main_sectors) or "无"
        lines.append(f"1. **市场状态（MRS*）**：{m.mrs_star}/10（{m.regime}，k={m.k}）")
        lines.append(f"2. **总仓位上限**：{m.position_cap[0]:.0%} - {m.position_cap[1]:.0%}")
        lines.append(f"3. **主线板块**：{main_txt}" +
                     (f" ｜ 次主线: " + "；".join(f"{s.etf}(SHS {s.shs})" for s in sub_sectors) if sub_sectors else ""))
        lines.append(f"4. **允许交易标的**：{len(r.picks)} 只 → " +
                     ("、".join(p.ticker for p in r.picks) if r.picks else
                      "币种执行边界受阻，仅供研究" if _execution_blocked(r.raw) else
                      "本轮无新增纸面买入计划" if company_note else
                      "无达标标的，等待"))
    lines.append("")
    overview = _markdown_text(r.market_view) if instructions is not None else r.market_view
    lines.extend([f"**系统指令**：`{r.action}` — {overview}", ""])
    if company_note:
        lines.extend([_markdown_text(company_note), ""])
    if instructions is not None:
        lines.extend(["## 模型最终判断与执行条件（纸面研究）", ""])
        if not instructions:
            lines.extend(["本轮没有新增单票指令。", ""])
        for item in instructions:
            action = "纸面买入计划" if item["action"] == "buy" else "观察"
            lines.extend([f"### {_markdown_text(item['symbol'])} · {action}", "",
                          _markdown_text(item["action_summary"]), ""])
            for label, value in model_instruction_fields(item):
                lines.append(f"- **{label}**：{_markdown_text(value)}")
            if item["sources"]:
                lines.extend(f"- **新闻来源**：{model_source_html(source, markdown=True)}"
                             for source in item["sources"])
            else:
                lines.append("- 本条未引用新闻。")
            lines.append("")

    # ---- MRS 明细 ----
    if m:
        lines.append("## 二、MRS 市场共振（第一道门）")
        lines.append("")
        lines.append("| 维度 | 得分 | 明细 |")
        lines.append("|---|---|---|")
        for key, d in m.dimensions.items():
            detail = d.evidence[0] if d.evidence else ""
            miss = f"（缺失: {', '.join(d.missing)}）" if d.missing else ""
            score_txt = d.score if d.score is not None else "缺失·已剔除"
            lines.append(f"| {key} | {score_txt} | {detail}{miss} |")
        lines.append("")
        lines.append(f"MRS_raw={m.mrs_raw} → Δ={m.delta}（可用维度极差）→ k={m.k} → **MRS*={m.mrs_star}**")
        lines.append("")

    # ---- SHS ----
    lines.append("## 三、SHS 板块热度（第二道门）")
    lines.append("")
    lines.append("| 板块ETF | SHS | 宏观 | 资金动量 | 叙事 | 微观 | 广度 | 池 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in r.sectors:
        pool = "主线" if s.in_main_pool else "次主线" if s.in_sub_pool else ""
        breadth = f"{s.breadth:.0f}%" if s.breadth == s.breadth else "N/A"
        f_ = lambda k: s.factors.get(k) if s.factors.get(k) is not None else "—"
        lines.append(f"| {s.etf} | {s.shs} | {f_('macro')} | {f_('flow')} "
                     f"| {f_('narr')} | {f_('micro')} | {breadth} | {pool} |")
    lines.append("")

    # ---- 产业链周期（新增模块）----
    lines.append("## 四、产业链周期 ICS（新增维度）")
    lines.append("")
    lines.append("| 产业链 | ICS | 周期阶段 | 领涨环节 | 链内广度 | 轮动信号 | 热区 |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in r.chains:
        breadth = f"{c.breadth:.0f}%" if c.breadth == c.breadth else "N/A"
        lines.append(f"| {c.name} | {c.ics} | {c.stage} | {c.leading_link} | {breadth} "
                     f"| {c.rotation_signal} | {'🔥' if c.hot else ''} |")
    lines.append("")
    lines.append("<details><summary>各链明细证据</summary>")
    lines.append("")
    for c in r.chains:
        for e in c.evidence:
            lines.append(f"- {e}")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # ---- 扫描统计 ----
    ss = r.raw.get("scan_stats", {})
    if ss:
        lines.append("## 五、本轮股票池扫描")
        lines.append("")
        lines.append(f"- 本轮扫描池: {ss.get('universe_size')} 只 → 通过硬过滤: {ss.get('passed')} 只 → 精评候选: {ss.get('selected')} 只")
        rej = ss.get("rejected", {})
        lines.append(f"- 过滤明细: 低价 {rej.get('price', 0)} ｜ 流动性不足 {rej.get('adv', 0)} ｜ "
                     f"历史不足 {rej.get('history', 0)} ｜ 波动病态 {rej.get('atr', 0)} ｜ 无数据 {rej.get('nodata', 0)}")
        top10 = ss.get("top10", [])
        if top10:
            lines.append(f"- 初排 Top10: " + "，".join(f"{t}({s})" for t, s in top10))
        lines.append("")

    # ---- TSS 精评 ----
    lines.append("## 六、TSS 精评（第三道门）")
    lines.append("")
    lines.append("| 标的 | TSS_final | 结构 | 动能 | 期权 | 模板 | 关键位 | 产业链 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for c in r.watchlist[:15]:
        s_opt = c.s_options if c.s_options is not None else "—"
        lines.append(f"| {c.ticker} | {c.tss_final} | {c.s_structure} | {c.s_momentum} "
                     f"| {s_opt} | {c.entry_template or '-'} | {c.key_level:.2f} | {c.chain_id or '-'} |")
    lines.append("")

    # ---- 交易卡片 ----
    lines.append("## 七、风控放行与交易卡片")
    lines.append("")
    debate = r.raw.get("debate", {}) or {}
    if r.picks:
        for p in r.picks:
            lines.append("```")
            lines.append(p.card)
            # v6.3 S2：交易卡片"第六段：多空辩论证据"（只读证据附加，
            # 辩论绝不修改任何分数与闸门输出；LLM 透传时如实标注）
            sec = _debate_card_section(p.ticker, debate) if instructions is None else ""
            if sec:
                lines.append(sec)
            lines.append("```")
            lines.append("")
    else:
        lines.append("无放行标的。")
        lines.append("")
    if debate.get("status") == "passthrough" and debate.get("triggered"):
        lines.append("> 本轮触发多空辩论的标的（"
                     + "、".join(debate["triggered"])
                     + "）因 LLM 不可用走红线透传：本轮无辩论证据。")
        lines.append("")

    # ---- 备注与缺失披露 ----
    lines.append("## 八、备注与数据缺失披露")
    lines.append("")
    for n in r.notes:
        lines.append(f"- {n}")
    lines.append("- 叙事兑现（EPS 修正/指引）自 v5.0 起由 LLM 驱动（NarrativeAgent）；"
                 "LLM 不可用时按红线透传并剔除再归一化，绝不用规则估算。")
    lines.append("- 微观（GEX/0DTE/Skew）免费源缺失：缺失因子在聚合时【剔除并再归一化】，"
                 "期权维度（PCR/IV 分位）随信号日记逐日积累满 10 个样本后自动启用真实评分。")
    if instructions is None:
        lines.append(f"- 多空辩论证据层（v6.3 S2）：仅灰区标的触发（每日上限 "
                     f"{config.DEBATE_MAX_PER_DAY} 场，HOLD 区高分线 "
                     f"{config.DEBATE_HOLD_TSS_MIN}），辩论结论只作交易卡片第六段证据，"
                     "不修改任何分数与闸门输出；回测路径禁用。")
    else:
        lines.append("- 本轮由模型在已通过风控的范围内作最终判断；"
                     "行情、股数、止损和风险预算取自同一次纸面计划。")
    # ---- S4 多市场披露：市场标识 / 基准缺失 / 板块广度缺失 / 合规校验 / 链覆盖率 ----
    if mk:
        lines.append("")
        lines.append("### 多市场披露（S4：框架不变、输入替换）")
        lines.append("")
        bmk = mk.get("benchmarks", {})
        lines.append(f"- 基准组：指数 {bmk.get('index_label', bmk.get('index'))}"
                     f"（{bmk.get('index')}）｜ 利率 {bmk.get('rate_label', bmk.get('rate'))}"
                     f"（{bmk.get('rate')}）｜ 波动率 {bmk.get('vol_label', bmk.get('vol'))}"
                     f"（{bmk.get('vol')}）")
        fresh = r.raw.get("freshness") or {}
        if fresh.get("benchmark_missing"):
            lines.append(f"- ⚠️ 基准缺失（剔除再归一化，不钉中性分）: "
                         f"{'、'.join(fresh['benchmark_missing'])}")
        if fresh.get("sector_breadth_missing"):
            lines.append(f"- ⚠️ {fresh['sector_breadth_missing']}")
        sf = mk.get("scan_filters", {})
        lines.append(f"- 扫描硬过滤：价格 ≥ {sf.get('min_price')} {sf.get('currency')} ｜ "
                     f"20 日成交额 ≥ {sf.get('min_adv'):,} {sf.get('currency')}")
        lines.append(f"- 产业链映射覆盖率（本市场）: {r.raw.get('chain_coverage', '—')}")
        comp = r.raw.get("compliance") or []
        violations = [c for c in comp if not c.get("allowed")]
        rules = "、".join(mk.get("compliance_rules", []))
        lines.append(f"- 合规规则（围栏前置）: {rules}")
        if violations:
            for v in violations:
                lines.append(f"  - ❌ {v.get('ticker')} {v.get('side')}: "
                             f"{v.get('reason')}（{v.get('rule_id')}）")
        elif _execution_blocked(r.raw):
            lines.append(f"  - 本轮市场规则校验 {len(comp)} 笔，未命中违反项；"
                         "币种边界仍阻止交易记账")
        else:
            lines.append(f"  - 本轮校验 {len(comp)} 笔买入请求，全部放行（无违反项）")
        if not mk_grad.get("graduated", True):
            lines.append(f"- D7 轻仓通道：新市场验证期（{mk_grad.get('settled', 0)}"
                         f"/{mk_grad.get('required', 50)}），全部放行标的强制轻仓 "
                         f"×{sum(config.MARKET_LIGHT_SIZE) / 2:.2f}"
                         "（满 50 笔结算且 DSR 过关才开放标准通道，门槛见 "
                         "config.MARKET_GRADUATION）")
    lines.append("")

    # ---- 数据可信度与交叉验证（v6.3 S3 数据层披露：规则层调度，不引入 LLM）----
    cv = r.raw.get("cross_validation") or {}
    if cv:
        lines.append("### 数据可信度与交叉验证（S3 Tiger Data Fabric）")
        lines.append("")
        lines.append(f"- 交叉验证统计：总文档 **{cv.get('total', 0)}** 篇 ｜ "
                     f"关键事件类 {cv.get('key_event_docs', 0)} 篇 ｜ "
                     f"通过（≥2源且至少一个≤T2）**{cv.get('corroborated', 0)}** 篇 ｜ "
                     f"被降级 **{cv.get('downgraded', 0)}** 篇")
        lines.append(f"- 其中 LLM 语义标注缺失记未验证 {cv.get('llm_missing', 0)} 篇（红线透传，不阻塞管线）；"
                     f"缺发布时间（Point-in-Time）{cv.get('missing_published_at', 0)} 篇")
        lines.append("- 口径：来源可信度 T0 监管原文 > T1 主流媒体 > T2 聚合门户 > T3 社媒"
                     "（config.SOURCE_TIERS/DOMAIN_TIERS 可覆盖）；被降级文档不删除，"
                     "决策侧仅作背景参考，叙事/舆情打分只使用 corroborated=True 集合。")
        ah = r.raw.get("ah_topics_enabled", False)
        lines.append(f"- A/H 市场情报主题（S4 铺路）：**{'已启用' if ah else '默认关闭'}**"
                     f"（config.AH_TOPICS_ENABLED={'True' if ah else 'False'}，"
                     "关闭时不产生任何抓取调用）。")
        lines.append("")

    # ---- 统计校准（v6.3 S2 迭代层披露：只进报告，不改任何闸门）----
    cal = r.raw.get("calibration", {}) or {}
    if cal:
        lines.append("### 统计校准（迭代层 · 只进报告不改闸门）")
        lines.append("")
        if cal.get("status") == "skipped":
            lines.append(f"- 本轮校准环节失败，跳过披露并记录：{cal.get('reason', '')}")
        else:
            lines.append(f"- {cal.get('disclosure', '')}")
            lines.append("")
            lines.append("| TSS 分桶 | 样本数 | 实际胜率 | Wilson 95% 区间 | 期望R |")
            lines.append("|---|---|---|---|---|")
            for b in cal.get("buckets", []):
                wr = f"{b['win_rate']:.1%}" if b.get("win_rate") is not None else "—"
                interval = (f"[{b['wilson'][0]:.1%}, {b['wilson'][1]:.1%}]"
                            if b.get("n") else "—")
                ar = b["avg_r"] if b.get("avg_r") is not None else "—"
                lines.append(f"| {b['bucket']} | {b['n']} | {wr} | {interval} | {ar} |")
            if cal.get("monotonic") is not None:
                lines.append("")
                lines.append(f"- 分数-结果单调性：{'✅ 成立' if cal['monotonic'] else '⚠️ 不成立（需复盘）'}"
                             f"（样本 {cal.get('n')} ≥ {cal.get('min_samples')}，输出校准曲线点）")
        lines.append("")
        lines.append(f"> 口径披露：样本门槛 {config.CALIBRATION_MIN_SAMPLES} 条"
                     f"（config.CALIBRATION_MIN_SAMPLES），样本库 "
                     f"{config.CALIBRATION_SAMPLES_PATH} 属会计账白名单，"
                     "与 journal 同级，禁止进入决策输入。")
        lines.append("")

    # ---- 科技产业链专项子集群（标准化汇入信号）----
    tech = r.raw.get("tech_signals", [])
    if tech:
        lines.append("## 九、科技产业链专项（子集群 → 主决策引擎）")
        lines.append("")
        lines.append(f"- 采集 {r.raw.get('docs_collected', 0)} 篇 → 清洗 {r.raw.get('docs_cleaned', 0)} 篇"
                     f"（语义标注缺失 {r.raw.get('docs_semantic_degraded', 0)} 篇，红线透传）")
        lines.append("")
        lines.append("| 子链 | 景气度 | 风险 | 加成 | 领涨环节 | 传导图 | 降级环节 |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in tech:
            trans = " ".join(f"{a}→{b}({v:.2f})" for a, tg in s.get("transmission", {}).items()
                             for b, v in tg.items()) or "—"
            deg = ",".join(s.get("degraded_components", [])) or "—"
            pros = s.get("prosperity")
            lines.append(f"| {s.get('chain_id')} | {pros if pros is not None else '—'} "
                         f"| {s.get('risk_level', 0):.0f} | ×{s.get('bonus_hint', 1.0)} "
                         f"| {s.get('leading_link') or '—'} | {trans} | {deg} |")
        lines.append("")
        alerts_all = [a for s in tech for a in s.get("alerts", [])]
        if alerts_all:
            lines.append("**风险预警**：")
            for a in alerts_all[:8]:
                lines.append(f"- ⚠️ [{a.get('severity'):.0f}/10·{a.get('type')}] "
                             f"{a.get('headline_zh')}（传导: {'/'.join(a.get('transmission', [])) or '—'}）")
            lines.append("")
        lines.append("<details><summary>各子链证据</summary>")
        lines.append("")
        for s in tech:
            for e in s.get("evidence", []):
                lines.append(f"- [{s.get('chain_id')}] {e}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---- 红线执行轨迹（全链路完整性审计）----
    redline = r.raw.get("redline", [])
    if redline:
        lines.append("## 十、红线执行轨迹（环节无一遗漏的机器证明）")
        lines.append("")
        lines.append("| 环节 | 状态 | 耗时ms | 备注 |")
        lines.append("|---|---|---|---|")
        for s in redline:
            mark = "✅" if s["status"] == "executed" else "🔁透传"
            lines.append(f"| {s['step']} | {mark} | {s['ms']} | {s.get('note', '')} |")
        lines.append("")

    lines.append("---")
    lines.append("*非预测、重流程、可审计。本报告不构成投资建议。*")
    return "\n".join(lines)
