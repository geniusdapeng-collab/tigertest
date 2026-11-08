"""HTML 报告生成器 — 自包含单文件（内嵌 CSS/SVG，零外部依赖）。

设计：深色金融终端风 + 金色强调；结论先行（action 徽章 + 四行看板），
图随文走（MRS 雷达 / SHS 条形 / 科技链景气度与传导图 / 红线轨迹 / 胜率看板）。

用法：
  库：  render_html(result, journal_stats=None, journal_entries=None) -> str
  CLI： python3 -m trading_system.report_html <result.json> [--journal journal.json] [-o out.html]
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
from pathlib import Path
import re as _re

from . import config, report_assets
from .data_models import PipelineResult
from .redline import STEP_REGISTRY
from .report import (
    _accounting_note, _execution_blocked, _freshness_note, _market_currency,
    _market_name, _universe_note, company_evidence_note, research_audit_tables,
    model_instruction_fields, model_instructions, model_source_html,
    report_research_status, research_security_fields, research_source_anchor,
    research_source_html, research_status_notes,
)
from .research_status import AXIS_LABELS, GATE_LABELS, GATE_STATES, state_label

try:  # AI 生成美术资源（老虎全球资产管理头图 / 产业链主题产品图，内嵌 base64，零外链）
    from .hero_art import HERO_ART_B64, THEME_ART_B64
except Exception:  # 资源缺失时降级为纯 SVG 视觉，报告照常生成
    HERO_ART_B64, THEME_ART_B64 = "", {}

# ---------------------------------------------------------------- 配色
BG = "#f4fae4"        # 嫩芽白底（淡绿调的白，嫩芽初生感）
CARD = "#fcfff2"
BORDER = "#d8e6b8"
FG = "#1c2a10"        # 深墨绿黑正文（亮底可读）
MUTED = "#5f7050"
GOLD = "#5b8c00"      # 主题强调文字色（荧光黄绿压深至可读区）
LIME = "#ccff00"      # 荧光黄绿：主按钮/进度条/大色块专用
GREEN = "#16a34a"     # 涨 / BUY 语义绿
RED = "#dc2626"
YELLOW = "#b45309"
BLUE = "#2563eb"


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _action_style(action: str) -> tuple[str, str]:
    return {
        "BUY": (GREEN, "标准做多"),
        "LIGHT": (YELLOW, "轻仓试错"),
        "HOLD": (BLUE, "持有观察"),
        "WAIT": (YELLOW, "等待信号"),
        "AVOID": (RED, "回避开仓"),
        "WAIT_EVIDENCE": (YELLOW, "等待证据"),
    }.get(action, (MUTED, action))


def _model_instruction_cards(instructions: list[dict]) -> str:
    cards = ["<h2>模型最终判断与执行条件（纸面研究）</h2>"]
    if not instructions:
        cards.append("<p class='card'>本轮没有新增单票指令。</p>")
    for item in instructions:
        action = "纸面买入计划" if item["action"] == "buy" else "观察"
        cards.append("<article class='card' style='margin-bottom:10px;overflow-wrap:anywhere'>"
                     f"<h3>{_esc(item['symbol'])} · {action}</h3>"
                     f"<p>{_esc(item['action_summary'])}</p><dl>")
        for label, value in model_instruction_fields(item):
            cards.append(f"<dt><b>{label}</b></dt><dd>{_esc(value)}</dd>")
        cards.append("</dl>")
        if item["sources"]:
            cards.append("<details><summary>新闻来源</summary><ul>")
            cards.extend(f"<li>{model_source_html(source)}</li>" for source in item["sources"])
            cards.append("</ul></details>")
        else:
            cards.append("<p class='sub'>本条未引用新闻。</p>")
        cards.append("</article>")
    return "".join(cards)


def _research_overview(status: dict) -> str:
    parts = ["<section class='research-overview' aria-labelledby='research-status-heading'>",
        "<div class='research-heading'><div><div class='eyebrow-l'>RESEARCH RECORD</div>"
        "<h2 id='research-status-heading'>研究进度与交付状态</h2></div>"
        "<span class='research-snapshot'>封存报告</span></div>",
        f"<p class='sub research-identity'>任务：{_esc(status['run_id'] or '原始报告未提供任务编号')}</p>",
        "<div class='research-axes'>"]
    for key, label in AXIS_LABELS.items():
        state = status["axes"][key]["state"]
        parts.append(f"<div class='research-axis' data-axis='{key}'><div class='sub'>{label}</div>"
                     f"<strong>{_esc(state_label(state))}</strong></div>")
    parts.append("</div><div class='research-notes'>")
    parts.extend(f"<p>{_esc(note)}</p>" for note in research_status_notes(status))
    parts.append("</div></section>")
    return "".join(parts)


def _research_security_cards(status: dict) -> str:
    rows = status["securities"]
    parts = ["<section class='research-security-panel' aria-labelledby='research-securities-heading'>",
        "<div class='research-heading'><div><div class='eyebrow-l'>EVERY SECURITY · 逐项留痕</div>"
        "<h2 id='research-securities-heading'>逐证券证据与决策</h2></div>"
        f"<span class='research-snapshot'>共 {len(rows)} 只</span></div>",
        "<p class='sub'>筛选只改变本报告的显示，不会启动研究、重新计算或更改账户。"
        "本页不能取消调度；停止观察与取消任务需要分别通过原任务入口处理。</p>",
        "<div class='research-filters'><label>搜索证券<input id='research-security-search' type='search' "
        "placeholder='输入代码或证券标识' oninput='filterResearchSecurities()'></label>"
        "<label>本轮状态<select id='research-security-filter' onchange='filterResearchSecurities()'>"
        "<option value='all'>显示全部</option>"]
    for state in ("WAIT_EVIDENCE", "PENDING_RESEARCH", "BUY", "HOLD", "NOT_SELECTED", "NOT_ASSESSED"):
        parts.append(f"<option value='{state}'>{_esc(state_label(state))}</option>")
    parts.append("</select></label><button class='research-reset' id='research-security-reset' "
                 "type='button' disabled onclick='resetResearchSecurities()'>清除筛选</button></div>"
                 f"<p class='sub' id='research-security-count' aria-live='polite'>显示 {len(rows)} / {len(rows)} 只证券</p>"
                 "<div id='research-security-empty' class='research-empty' hidden>没有符合筛选条件的证券。"
                 "清除筛选可恢复全部原始记录。</div>")
    if not rows:
        parts.append("<div class='research-empty'>本报告没有逐证券记录；缺少记录不能解释为研究后否决或已经空仓。</div>")
    for row in rows:
        state = row["state"]
        parts.append(f"<article class='research-security-card' data-research-security data-state='{_esc(state)}' "
                     f"data-search='{_esc(row['symbol'] + ' ' + (row['security_id'] or ''))}'>"
                     f"<div class='research-card-heading'><h3>{_esc(row['symbol'])}</h3>"
                     f"<span class='research-state state-{_esc(state.lower())}'>{_esc(state_label(state))}</span></div>"
                     "<dl class='research-fields'>")
        for label, value in research_security_fields(row):
            parts.append(f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>")
        parts.append("</dl>")
        if row["gates"]:
            parts.append("<details class='research-details'><summary>查看逐关依据</summary>"
                         "<div class='research-gates'>")
            for gate, detail in row["gates"].items():
                parts.append(f"<div><strong>{_esc(GATE_LABELS.get(gate, gate))}</strong>"
                             f"<span>{_esc(GATE_STATES[detail['status']])}</span>"
                             f"<p class='sub'>{_esc('；'.join(detail['reasons']) or '未记录其他原因')}</p></div>")
            parts.append("</div></details>")
        if row["source_ids"]:
            parts.append("<div class='research-source-links'><b>原文导航</b>：" + " · ".join(
                f"<a href='#{research_source_anchor(identity)}'>{_esc(identity)}</a>" for identity in row["source_ids"]) + "</div>")
        else:
            parts.append("<p class='sub'>原文导航：本证券未记录可引用原文。</p>")
        parts.append("</article>")
    parts.append("<div class='research-source-directory'><h2>完整来源目录</h2>"
                 "<p class='sub'>保留本报告提供的每一份冻结原文与执行引用；部分原文仍可能缺失或不合格。"
                 "来源目录不会把摘要升级为完整原文。</p>")
    if not status["sources"]:
        parts.append("<div class='research-empty'>本报告未提供来源目录；不能据此认定没有相关信息。</div>")
    for source in status["sources"]:
        parts.append(f"<article class='research-source' id='{research_source_anchor(source['id'])}'>"
                     f"<h3>{_esc(source['id'])}</h3><p>{research_source_html(source)}</p>"
                     "<dl class='research-fields'>"
                     f"<dt>来源</dt><dd>{_esc(source.get('source_id') or '本报告未提供')}</dd>"
                     f"<dt>提取完整性</dt><dd>{_esc(source.get('completeness') or '未知')}</dd>"
                     f"<dt>内容校验</dt><dd class='research-hash'>{_esc(source.get('content_sha256') or '本报告未提供')}</dd>"
                     "</dl></article>")
    parts.append("</div></section>")
    return "".join(parts)


# ---------------------------------------------------------------- SVG 组件
def _get(v, key, default=None):
    """dict 与 dataclass 双兼容取值。"""
    if isinstance(v, dict):
        return v.get(key, default)
    return getattr(v, key, default)


def _number(value) -> float | None:
    """缺失或无效读数不能在显示层变成有效评分/资金。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _radar_svg(dims: dict, size: int = 300) -> str:
    """市场环境五维雷达图。dims: {key: DimensionScore/dict}，取 score 0-10。"""
    items = [(_DIM_ZH.get(k, k), _number(_get(v, "score")))
             for k, v in dims.items()][:6]
    if not items:
        return ""
    n = len(items)
    cx, cy, r = size / 2, size / 2 + 6, size / 2 - 46

    def pt(i: int, val: float) -> tuple[float, float]:
        ang = -math.pi / 2 + 2 * math.pi * i / n
        return cx + r * val * math.cos(ang), cy + r * val * math.sin(ang)

    parts = [f'<svg viewBox="0 0 {size} {size + 12}" width="{size}" '
             f'height="{size + 12}" role="img">']
    for frac in (0.34, 0.67, 1.0):
        pts = " ".join(f"{pt(i, frac)[0]:.1f},{pt(i, frac)[1]:.1f}" for i in range(n))
        parts.append(f'<polygon points="{pts}" fill="none" stroke="{BORDER}" stroke-width="1"/>')
    for i in range(n):
        x, y = pt(i, 1.0)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="{BORDER}"/>')
    # 缺失轴只保留轴与文字，既不补分，也不连接成完整面积。
    if all(s is not None for _, s in items):
        data_pts = " ".join(f"{pt(i, s / 10.0)[0]:.1f},{pt(i, s / 10.0)[1]:.1f}"
                            for i, (_, s) in enumerate(items))
        parts.append(f'<polygon points="{data_pts}" fill="{GOLD}33" stroke="{GOLD}" stroke-width="2"/>')
    for i, (k, s) in enumerate(items):
        if s is not None:
            x, y = pt(i, s / 10.0)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{GOLD}"/>')
        lx, ly = pt(i, 1.22)
        anchor = "middle" if abs(lx - cx) < 12 else ("start" if lx > cx else "end")
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" fill="{FG}" font-size="12" '
                     f'text-anchor="{anchor}">{_esc(k)}</text>')
        parts.append(f'<text x="{lx:.1f}" y="{ly + 14:.1f}" fill="{MUTED}" font-size="11" '
                     f'text-anchor="{anchor}">{s if s is not None else "缺失"}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _bar_row(label: str, value: float, vmax: float = 10.0, color: str = GOLD,
             extra: str = "") -> str:
    pct = max(0.0, min(100.0, value / vmax * 100))
    return (f'<div class="bar-row"><div class="bar-label">{_esc(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;'
            f'background:{color}"></div></div>'
            f'<div class="bar-val">{value}{extra}</div></div>')


def _transmission_svg(sig: dict) -> str:
    """子链传导图：三环节节点 + 箭头（粗细=传导强度），领涨环节金色高亮。"""
    links = ["upstream", "midstream", "downstream"]
    zh = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}
    lead = sig.get("leading_link")
    trans = sig.get("transmission") or {}
    w, h = 300, 64
    xs = {"upstream": 52, "midstream": 150, "downstream": 248}
    parts = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}">']
    parts.append(f'<defs><marker id="ah" markerWidth="7" markerHeight="7" refX="6" refY="3" '
                 f'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="{MUTED}"/></marker></defs>')
    for a, tg in trans.items():
        for b, strength in tg.items():
            if a in xs and b in xs:
                sw = 1 + 3.5 * float(strength)
                op = 0.35 + 0.65 * float(strength)
                parts.append(f'<line x1="{xs[a] + 34}" y1="{h / 2}" x2="{xs[b] - 38}" y2="{h / 2}" '
                             f'stroke="{GOLD}" stroke-width="{sw:.1f}" opacity="{op:.2f}" '
                             f'marker-end="url(#ah)"/>')
                mx = (xs[a] + xs[b]) / 2
                lvl = "强" if strength >= 0.66 else ("中" if strength >= 0.33 else "弱")
                parts.append(f'<text x="{mx}" y="{h / 2 - 8}" fill="{MUTED}" font-size="10" '
                             f'text-anchor="middle">传导{lvl}</text>')
    for lk in links:
        hot = (lk == lead)
        fill = "#e9f7c0" if hot else CARD
        stroke = GOLD if hot else BORDER
        parts.append(f'<rect x="{xs[lk] - 36}" y="{h / 2 - 16}" width="72" height="32" rx="7" '
                     f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
        parts.append(f'<text x="{xs[lk]}" y="{h / 2 + 4}" fill="{FG}" font-size="12" '
                     f'text-anchor="middle">{zh[lk]}{"★" if hot else ""}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _r_curve_svg(entries: list[dict], width: int = 560, height: int = 120) -> str:
    """胜率追踪：逐笔 R 累积迷你曲线。"""
    closed = [e for e in entries if e.get("r") is not None]
    if len(closed) < 2:
        return ""
    cum, ys = 0.0, [0.0]
    for e in closed:
        cum += float(e["r"])
        ys.append(cum)
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    step = width / max(1, len(ys) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 14 - (v - lo) / span * (height - 30):.1f}"
                   for i, v in enumerate(ys))
    zero_y = height - 14 - (0 - lo) / span * (height - 30)
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">'
            f'<line x1="0" y1="{zero_y:.1f}" x2="{width}" y2="{zero_y:.1f}" '
            f'stroke="{BORDER}" stroke-dasharray="4 3"/>'
            f'<polyline points="{pts}" fill="none" stroke="{GOLD}" stroke-width="2"/></svg>')


# ---------------------------------------------------------------- 决策依据（业务语言）
def _sec(title: str, body: list[str], open_: bool = True) -> str:
    return (f"<details{' open' if open_ else ''} style='margin-top:10px'>"
            f"<summary><b>{_esc(title)}</b></summary>"
            f"<div style='margin-top:8px'>{''.join(body)}</div></details>")


def _kv(k: str, v: str) -> str:
    return f"<div class='sub' style='margin:2px 0'>· {k}：<b style='color:{FG}'>{v}</b></div>"


_DIM_ZH = {"macro": "宏观利率", "tech": "技术结构", "flow": "资金广度",
           "sent": "市场情绪", "micro": "微观结构",
           "narr": "叙事兑现", "options": "衍生品"}
_FACTOR_ZH = {"macro": "宏观环境适配", "flow": "资金持续流入",
              "narr": "产业叙事催化", "micro": "微观结构改善"}
_TEMPLATE_ZH = {"A": "突破后回踩确认", "B": "收缩后放量启动", "C": "趋势回撤企稳"}

# ---------------------------------------------------------------- 投资人视角中文化
_LINK_ZH = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}
_CHAIN_ZH = {"memory": "存储产业链", "logic": "逻辑芯片链", "foundry": "晶圆代工链",
             "equipment": "半导体设备链", "ai_model": "AI 模型/云链", "ai_app": "AI 应用链"}
_ETF_ZH = {"SMH": "半导体", "XLK": "科技", "XLI": "工业", "XLF": "金融", "XLE": "能源",
           "XLV": "医疗保健", "XLP": "必需消费", "XLY": "可选消费", "XLB": "材料",
           "XLU": "公用事业", "XLRE": "房地产", "XLC": "通信服务", "IBB": "生物科技",
           "IWM": "小盘股", "QQQ": "纳指100", "SPY": "标普500"}
_STEP_ZH = {"search.collect": "情报采集", "clean.rule_base": "规则清洗",
            "clean.llm_semantic": "AI 语义清洗", "data.prepare": "行情准备",
            "tech.monitor": "科技链监控", "tech.cycle_linkage": "周期联动分析",
            "tech.sentiment": "舆情分析", "tech.risk": "风险扫描",
            "tech.fusion": "多源融合", "sector.narrative": "板块叙事",
            "layer1.mrs": "市场环境评估", "layer2.sector": "板块评估",
            "layer2b.chain": "产业链评估", "layer0.scan": "股票池扫描",
            "layer3.tss": "个股质量评估", "layer4.risk": "风控闸门",
            "report.emit": "报告生成"}
_REJ_ZH = {"price": "股价过低、风险偏高", "adv": "日均成交额不足、流动性差",
           "history": "上市时间太短、数据不足", "atr": "波动过大、风险不可控",
           "nodata": "无有效行情数据"}
_PROVIDER_ZH = {"demo": "离线合成演示数据", "yahoo": "Yahoo 日线行情",
                "stooq": "Stooq 日线行情", "agentgw": "服务端行情通道",
                "ifind_gw": "同花顺 iFinD 行情", "tiingo": "Tiingo 日线行情"}
_SRC_SHORT = {"demo": "离线合成", "yahoo": "Yahoo 财经", "stooq": "Stooq",
              "agentgw": "服务端通道", "ifind_gw": "同花顺 iFinD",
              "tiingo": "Tiingo"}
# 常见标的中文名（展示层静态表；未收录的代码优雅回退为仅显代码）
_TICKER_ZH = {
    # 半导体·存储·设备
    "NVDA": "英伟达", "AMD": "超威半导体", "INTC": "英特尔", "QCOM": "高通",
    "AVGO": "博通", "TXN": "德州仪器", "MRVL": "迈威尔科技", "MU": "美光科技",
    "STX": "希捷科技", "WDC": "西部数据", "SNDK": "闪迪", "LRCX": "泛林集团",
    "AMAT": "应用材料", "KLAC": "科磊", "ASML": "阿斯麦", "TSM": "台积电",
    "UMC": "联华电子", "GFS": "格芯", "ARM": "安谋控股", "MCHP": "微芯科技",
    "ON": "安森美", "NXPI": "恩智浦", "SWKS": "思佳讯", "TER": "泰瑞达",
    "LSCC": "莱迪思半导体", "MPWR": "芯源系统", "RMBS": "蓝铂世",
    # 硬件·服务器·网络
    "SMCI": "超微电脑", "DELL": "戴尔科技", "HPQ": "惠普", "HPE": "慧与",
    "ANET": "Arista 网络", "CSCO": "思科", "IBM": "IBM",
    # AI·软件·云
    "MSFT": "微软", "AAPL": "苹果", "GOOGL": "谷歌", "GOOG": "谷歌",
    "AMZN": "亚马逊", "META": "Meta 平台", "NFLX": "奈飞", "ORCL": "甲骨文",
    "CRM": "赛富时", "ADBE": "奥多比", "NOW": "ServiceNow", "PLTR": "Palantir",
    "SNOW": "Snowflake", "DDOG": "Datadog", "MDB": "MongoDB", "INTU": "财捷",
    "WDAY": "Workday", "TEAM": "Atlassian", "SHOP": "Shopify",
    "CRWD": "CrowdStrike", "PANW": "派拓网络", "ZS": "Zscaler",
    "NET": "Cloudflare", "FTNT": "飞塔信息", "OKTA": "Okta",
    # 消费·平台·金融
    "TSLA": "特斯拉", "UBER": "优步", "ABNB": "爱彼迎", "COIN": "Coinbase",
    "HOOD": "Robinhood", "PYPL": "贝宝", "V": "维萨", "MA": "万事达",
    "JPM": "摩根大通", "DIS": "迪士尼", "WMT": "沃尔玛", "COST": "好市多",
    "MCD": "麦当劳", "NKE": "耐克", "SBUX": "星巴克", "RBLX": "Roblox",
    "EA": "艺电", "TTWO": "Take-Two",
    # 工业·能源·医药
    "CAT": "卡特彼勒", "DE": "迪尔股份", "BA": "波音", "GE": "通用电气",
    "HON": "霍尼韦尔", "XOM": "埃克森美孚", "CVX": "雪佛龙", "LLY": "礼来",
    "UNH": "联合健康", "JNJ": "强生", "PFE": "辉瑞", "MRK": "默克",
    "ABBV": "艾伯维",
    # 中概
    "BABA": "阿里巴巴", "PDD": "拼多多", "JD": "京东", "BIDU": "百度",
    "NTES": "网易", "TCOM": "携程", "BILI": "哔哩哔哩", "BEKE": "贝壳",
    "NIO": "蔚来", "XPEV": "小鹏汽车", "LI": "理想汽车",
    # 新能源
    "ENPH": "Enphase 能源", "SEDG": "SolarEdge", "FSLR": "第一太阳能",
}
# 公司一句话（它是谁、靠什么赚钱——交易故事卡的 WHO 段；未收录自动省略）
_TICKER_BIO = {
    "NVDA": "AI 时代的卖铲人——全世界训练大模型，都得先排队买它的 GPU。",
    "AMD": "GPU 老二，专啃英伟达吃不下的性价比市场。",
    "INTC": "曾经的芯片霸主，如今在代工与 AI 之间找第二春。",
    "QCOM": "手机芯片地主，正在把基带租金收到汽车和 PC 上。",
    "AVGO": "定制 AI 芯片的隐形包工头，谷歌们的自研芯片多出自它手。",
    "TXN": "模拟芯片老铺，工业与汽车电子的现金牛。",
    "MRVL": "数据中心互联芯片供应商，AI 机房里的布线专家。",
    "MU": "美国独苗存储厂，HBM 高带宽内存是 AI 芯片的粮食。",
    "STX": "全球硬盘双雄之一——AI 越能生成，世界越需要地方存。",
    "WDC": "硬盘双雄另一位，分拆闪迪后专心吃容量红利。",
    "SNDK": "闪存老牌，从西部数据分拆单飞，押注 AI 端侧存储。",
    "LRCX": "刻蚀机霸主，存储扩产潮里最确定的收租人。",
    "AMAT": "半导体设备超市，芯片厂扩产绕不开的一站式货架。",
    "KLAC": "良率检测警察，芯片出厂前都得过它的安检门。",
    "ASML": "光刻机唯一卖家，EUV 一台顶一架波音，没有替代。",
    "TSM": "全球芯片代工厂之王，苹果英伟达的图纸都在这落地。",
    "UMC": "成熟制程代工老将，赚的是够用就好的钱。",
    "GFS": "美国本土代工独苗，吃政策饭的特色工艺厂。",
    "ARM": "芯片架构收租鼻祖，每部手机都在给它交授权费。",
    "SMCI": "AI 服务器装机快手，英伟达显卡的组装车间。",
    "DELL": "企业 IT 老管家，AI 服务器让它老树发新芽。",
    "ANET": "数据中心交换机新贵，AI 集群的神经网络铺设者。",
    "MSFT": "云与 Office 双地主，Copilot 把 AI 塞进每个工位。",
    "AAPL": "硬件生态闭环之王，AI 故事的底牌是十亿台设备。",
    "GOOGL": "搜索现金牛 + 全栈 AI，Gemini 与 TPU 两条腿走路。",
    "AMZN": "云老大 AWS 撑起利润，AI 基建最壕的甲方。",
    "META": "社交广告印钞机，开源模型路线的旗手。",
    "NFLX": "流媒体一哥，广告套餐打开第二增长曲线。",
    "ORCL": "数据库老财主，靠 AI 云订单焕发第二春。",
    "CRM": "企业软件管家，押注 AI 代理替人干活。",
    "PLTR": "给政府和巨头装 AI 大脑的公司，神秘但订单硬核。",
    "SNOW": "数据仓库房东，AI 时代的数据地基。",
    "CRWD": "云时代保安队长，终端安全的订阅制收租人。",
    "PANW": "防火墙老大哥，安全平台化的集大成者。",
    "TSLA": "电动车教父，估值押注在自动驾驶与机器人上。",
    "CAT": "全球工程机械一哥，基建与矿山的风向标。",
    "UBER": "出行与外卖双平台，自动驾驶时代的中介费赢家。",
    "COIN": "加密交易所头牌，币圈情绪的晴雨表。",
    "LLY": "减肥药双雄之一，GLP-1 让它成了医药股里的科技股。",
    "COST": "会员制零售堡垒，通胀时代消费者的避风港。",
}


def _src_zh(src: str) -> str:
    """行情血缘源名 → 投资人可读短名（永不出现裸 demo 等技术 ID）。"""
    return _SRC_SHORT.get(src, src)


def _chain_zh(chain_id: str | None) -> str:
    """产业链英文 ID → 中文名（先查科技子链注册表，再查主链注册表，失败用内置表）。"""
    if not chain_id:
        return "-"
    try:
        from .tech_chain.universe import TECH_SUBCHAINS
        node = TECH_SUBCHAINS.get(chain_id)
        if node and node.get("name"):
            return node["name"]
    except Exception:
        pass
    try:
        from .chains import CHAINS
        node = CHAINS.get(chain_id)
        if node and node.get("name"):
            return node["name"]
    except Exception:
        pass
    return _CHAIN_ZH.get(chain_id, chain_id)


def _etf_zh(etf: str) -> str:
    """ETF 代码 → 中文板块名（代码保留在括号内，便于对照行情软件）。"""
    zh = _ETF_ZH.get(etf)
    return f"{zh}（{etf}）" if zh else etf


def _name_zh(ticker: str) -> str:
    """标的代码 → 中文公司名（未收录返回空串，展示层自行回退）。"""
    return _TICKER_ZH.get((ticker or "").upper(), "")


def _prosperity_label(score) -> str:
    """景气度 → 业务档位词。"""
    if score is None:
        return "未知"
    if score >= 7.5:
        return "火热"
    if score >= 6.0:
        return "升温"
    if score >= 4.5:
        return "平稳"
    return "降温"


def _risk_label(risk) -> tuple[str, str]:
    """风险值 → (业务档位, 颜色)。"""
    if risk is None:
        return "未知", MUTED
    if risk >= 7:
        return "高", RED
    if risk >= 4:
        return "中", YELLOW
    return "低", GREEN


def _evidence_zh(text: str) -> str:
    """证据串里的英文残留翻译成业务中文。"""
    out = str(text)
    for en, zh in _LINK_ZH.items():
        out = out.replace(en, zh)
    return (out.replace("相对强度映射", "联动强度")
               .replace("领涨环节", "领涨环节："))



def _health(score, missing: bool = False) -> str:
    """分数 → 业务状态词（不暴露内部阈值与权重）。"""
    score = _number(score)
    if missing or score is None:
        return "数据缺失"
    if score >= 7.5:
        return "强"
    if score >= 6.0:
        return "偏强"
    if score >= 4.5:
        return "中性"
    return "偏弱"


def _dim_reading(name: str, score) -> str:
    """按维度生成业务解读（不含内部参数）。"""
    h = _health(score)
    base = {
        "macro": "利率与流动性环境",
        "tech": "大盘趋势与关键位结构",
        "flow": "市场广度与资金参与面",
        "sent": "投资者情绪与风险偏好",
        "micro": "波动率与微观交易结构",
    }.get(name, name)
    tail = {"强": "构成明确支撑", "偏强": "总体有利", "中性": "影响中性",
            "偏弱": "构成一定压力", "数据缺失": "本轮不可用，已剔除并再归一化"}[h]
    return f"{base}{tail}"


def _harmony(mrs) -> str:
    available = sum(_number(_get(d, "score")) is not None
                    for d in mrs.dimensions.values())
    missing = len(mrs.dimensions) - available
    level = ("高（可用评分接近）" if mrs.delta < 4 else
             "中（可用评分存在分化）" if mrs.delta <= 6 else
             "低（可用评分分化较大，评级取保守档）")
    return (f"可用维度协调度：{level}；可用 {available} 个，缺失维度 {missing} 个"
            "（缺失维度已剔除并再归一化）")


def _scrub(text: str) -> str:
    """证据脱敏：核心系数/内部评分键/权重构成/子项打分不外显（投资者版纪律）。"""
    t = _re.sub(r"×\s*\d+(?:\.\d+)?", "×*", str(text))          # 乘性系数
    t = _re.sub(r"(bonus_hint|仓位系数)\s*[=:]?\s*[\d.]+", "", t)
    t = _re.sub(r"\s*→\s*TSS_final=[\d.]+", "", t)              # 内部评分链
    t = _re.sub(r"TSS_final=[\d.]+", "", t)
    t = _re.sub(r"TNX趋势=\w+→\d+分", "宏观利率适配已计入", t)
    t = _re.sub(r"⇒\s*S_\w+\s*=\s*[\d.]+", "⇒ 综合评定", t)     # 子项评分汇总
    t = _re.sub(r"S_(structure|momentum|options)\s*=\s*[\d.]+", "", t)
    t = _re.sub(r"[（(]\s*\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?){2,}\s*[）)]",
                "（*/*/*）", t)                                  # 权重构成 (0.4/0.4/0.2)
    t = _re.sub(r"→\s*-?\d+(?:\.\d+)?\s*分", "→已计入", t)        # 子项打分 →10分
    t = _re.sub(r"(SHS|TSS|ICS|MRS\*?)\s*=\s*[\d.]+", "", t)    # 内部评分键值
    return t


def _rationale_detail(r: "PipelineResult", pick, cand, sec, chain, tech_hit) -> str:
    """五段式决策依据（投资者业务语言）：结论、理由、计划、失效条件。

    内部推导与参数权重属核心机密，不对外展示；原始证据收入折叠审计底稿。
    数据全部来自本轮 pipeline 结构化透传，报告层不反推。
    """
    ra = (r.raw or {}).get("pick_rationale", {}).get(pick.ticker, {})
    mrs = r.mrs
    currency = _esc(_market_currency(r.raw))
    P: list[str] = []

    # ① 市场环境体检
    if mrs:
        b: list[str] = []
        cap = mrs.position_cap if isinstance(mrs.position_cap, (tuple, list)) else (0.0, 0.0)
        harmony = _harmony(mrs)
        b.append(_kv("总体结论", f"综合 <b>{mrs.mrs_star}/10</b> —— "
                                f"{'允许开新仓' if mrs.allow_new_positions else '暂不允许开新仓'}"
                                f"，建议总仓位 {cap[0]:.0%}–{cap[1]:.0%}；{harmony}"))
        b.append("<table style='margin-top:6px'><tr><th>体检维度</th><th>状态</th>"
                 "<th>解读</th></tr>")
        for name, d in mrs.dimensions.items():
            score = _get(d, "score")
            zh = _DIM_ZH.get(name, name)
            miss = bool(_get(d, "missing", []))
            b.append(f"<tr><td>{zh}</td><td><b>{_health(score, miss and score is None)}</b>"
                     f"</td><td class='sub'>{_esc(_dim_reading(name, score))}</td></tr>")
        b.append("</table>")
        P.append(_sec("① 市场环境体检（第一关）", b))

    # ② 板块选择理由
    if sec:
        b = []
        f = sec.factors or {}
        driver = max(((k, v) for k, v in f.items() if isinstance(v, (int, float))),
                     key=lambda x: x[1], default=None)
        b.append(_kv("板块热度", f"{_etf_zh(sec.etf)} <b>{sec.shs}/10</b> —— "
                                f"近 20 日相对大盘 {sec.r20:+.1f}%，板块内 "
                                f"{sec.breadth:.0f}% 个股处于中期上升趋势（健康线 60%）"))
        if driver:
            b.append(_kv("主要驱动", _FACTOR_ZH.get(driver[0], driver[0])))
        if ra:
            g = ra.get("gate", {}).get("shs", {})
            chan = ("当前最强主线之一" if g.get("main_pool") else
                    "虽未列入最强主线，但所属产业链处于景气热区，板块质量仍达放行标准"
                    if g.get("hot_channel") else "未达主线标准")
            b.append(_kv("主线地位", chan))
        P.append(_sec(f"② 板块选择理由（第二关 · {_etf_zh(sec.etf)}）", b))

    # ③ 产业链景气
    if chain:
        b = []
        zh = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}
        b.append(_kv("景气定位", f"{chain.name}处于<b>{chain.stage}</b>阶段"
                                f"（景气 {chain.ics}/10），当前由"
                                f"{zh.get(chain.leading_link, chain.leading_link)}环节领涨"
                                f"{'，属于本轮景气热区 🔥' if chain.hot else ''}"))
        b.append(_kv("链内健康度", f"{chain.breadth:.0f}% 成分股位于中期均线上方 ｜ "
                                  f"轮动状态：{chain.rotation_signal or '平稳'}"))
        b.append("<table style='margin-top:6px'><tr><th>环节</th><th>近20日表现</th>"
                 "<th>相对大盘</th></tr>")
        for lk, cl in chain.links.items():
            b.append(f"<tr><td>{zh.get(lk, lk)}{' ★领涨' if lk == chain.leading_link else ''}</td>"
                     f"<td>{_get(cl, 'momentum', float('nan')):+.1f}%</td>"
                     f"<td>{_get(cl, 'rs_20', float('nan')):+.1f}%</td></tr>")
        b.append("</table>")
        if tech_hit:
            risk_txt, _rc = _risk_label(tech_hit.get("risk_level"))
            b.append(_kv("科技赛道加成", f"所属{_chain_zh(tech_hit.get('chain_id'))}"
                                    f"景气{_prosperity_label(tech_hit.get('prosperity'))}，"
                                    "为该方向评分提供额外支撑"
                                    f"{'；注意：赛道风险偏高，支撑力度可能随时撤销' if risk_txt == '高' else ''}"))
        P.append(_sec("③ 产业链景气（第 2.5 关）", b))

    # ④ 个股质地
    if cand:
        b = []
        opt_note = ("衍生品数据本轮不可用，已剔除并再归一化" if cand.s_options is None
                    else f"衍生品维度 {cand.s_options}/10")
        b.append(_kv("综合质量", f"<b>{cand.tss_final}/10</b> —— 价格结构 "
                                f"{cand.s_structure}/10、动能 {cand.s_momentum}/10；{opt_note}"))
        b.append(_kv("流动性与波动", f"日K收盘价 {currency} {cand.price:.2f}，日均成交额约 "
                                    f"{currency} {cand.adv_usd / 1e6:.0f}M，"
                                    f"日均波动 {cand.atr_pct:.1%}"))
        tpl = _TEMPLATE_ZH.get(cand.entry_template, cand.entry_template or "待定")
        b.append(_kv("入场形态", f"{tpl} ｜ 关键位 {cand.key_level:.2f}"))
        if cand.stop_plan:
            b.append(_kv("止损计划", cand.stop_plan))
        P.append(_sec("④ 个股质地（第三关）", b))

    # ⑤ 交易计划与风控
    if ra:
        b = []
        mode_note = ("三道关全部通过，按标准仓位执行" if ra.get("standard")
                     else "轻仓试探性质，仓位已按比例压降")
        b.append(_kv("计划性质", f"{ra.get('mode')} —— {mode_note}"))
        pos_usd = ra.get("shares", 0) * (ra.get("entry") or 0)
        b.append(_kv("投入与仓位", f"计划 {ra.get('shares')} 股（约 ${pos_usd:,.0f}，"
                                  f"占账户 {ra.get('position_pct', 0):.1%}"
                                  f"{'；已触及单票仓位上限并自动压降' if ra.get('position_capped') else ''}）"))
        b.append(_kv("计划风险", f"入场参考 {ra.get('entry')}，止损 {ra.get('stop')} —— "
                                f"按计划价差计算的风险预算 "
                                f"<b>${ra.get('r_usd', 0):,.0f}</b>；"
                                "跳空、滑点与佣金可能使实际亏损超过计划风险"))
        rank = next((i + 1 for i, p in enumerate(r.picks) if p.ticker == pick.ticker), None)
        if rank:
            b.append(_kv("今日优先级", f"第 {rank} 位（共 {len(r.picks)} 只放行，"
                                      "按市场×板块×个股×流动性综合排序）"))
        P.append(_sec("⑤ 交易计划与风控", b))

    # ⑥ 失效与离场条件
    b = []
    if ra:
        b.append(_kv("价格离场", f"收盘跌破止损 {ra.get('stop')}，无条件离场"))
    b.append(_kv("时间离场", "入场后 5–7 个交易日未推进或跑输所属板块，降仓或换股"))
    b.append(_kv("盈利保护", "浮盈达到两倍风险后，止损上移至成本线/最近支撑"))
    inv = ["市场环境综合评级转弱", "所属板块掉出主线且产业链热区熄灭",
           "个股综合质量不再达标"]
    if tech_hit and (tech_hit.get("risk_level") or 0) >= 7:
        inv.append("科技子链风险继续升级（评分支撑将被撤销）")
    b.append(_kv("结论失效", "；".join(inv)))
    P.append(_sec("⑥ 失效与离场条件（纪律优先于观点）", b))

    # —— 系统审计底稿（内部证据记录，折叠不展开；核心系数脱敏）——
    audit: list[str] = []
    for label, obj in (("个股", cand), ("板块", sec), ("产业链", chain)):
        for e in (_get(obj, "evidence", []) or [])[:8]:
            audit.append(f"<div class='sub'>[{label}] {_esc(_scrub(e))}</div>")
    if audit:
        P.append(f"<details style='margin-top:8px'><summary class='sub'>系统审计底稿"
                 f"（内部证据记录，{len(audit)} 条）</summary>"
                 f"<div style='margin-top:6px'>{''.join(audit)}</div></details>")
    return "".join(P)


# ---------------------------------------------------------------- 决策卡
def _gate_row(name: str, value: str, threshold: str, ok: bool) -> str:
    mark = f"<span class='ok'>✓</span>" if ok else f"<span style='color:{RED}'>✗</span>"
    return (f"<tr><td>{_esc(name)}</td><td><b>{_esc(value)}</b></td>"
            f"<td class='sub'>{_esc(threshold)}</td><td>{mark}</td></tr>")


def _pick_context(r: "PipelineResult", pick):
    """标的相关联的候选档案/板块/产业链/科技赛道信号（决策卡与深度报告共用）。"""
    cand = next((c for c in r.watchlist if c.ticker == pick.ticker), None)
    sec = next((s for s in r.sectors
                if s.etf == (cand.sector_etf if cand else pick.sector)), None)
    chain = next((c for c in r.chains
                  if c.chain_id == (cand.chain_id if cand else pick.chain)), None)
    tech = (r.raw or {}).get("tech_signals", [])
    tech_hit = next((s for s in tech if s.get("main_chain") == pick.chain
                     and abs(s.get("bonus_hint", 1.0) - 1.0) > 1e-6), None)
    return cand, sec, chain, tech_hit


def _decision_card(r: "PipelineResult", pick) -> str:
    """基金经理视角决策卡：放行的是谁、为什么、怎么判出来的、怎么进怎么出。"""
    cand, sec, chain, tech_hit = _pick_context(r, pick)
    mrs = r.mrs
    currency = _esc(_market_currency(r.raw))

    P: list[str] = []
    # —— 标题与一句话结论（业务语言）——
    stop_pct = ((pick.stop_price / pick.entry_price - 1.0) * 100.0
                if pick.entry_price else 0.0)
    concl = ((f"{chain.name}处于{chain.stage}阶段{'、景气热区🔥' if chain.hot else ''}"
              if chain else _chain_zh(pick.chain))
             + (f" × {_etf_zh(sec.etf)} 板块"
                f"{'（当前最强主线之一🔥）' if sec.in_main_pool else '（热区链支撑）'}"
                if sec else "")
             + f" × 个股综合质量 {pick.tss_final}/10"
             + " —— 方向对、赛道热、质地硬，放行。")
    P.append("<div class='card' style='border-color:" + GOLD + "66'>")
    # —— THE TRADE：一句话说清这笔买卖（故事卡开场）——
    nm_tk = _name_zh(pick.ticker)
    P.append("<div class='eyebrow-l'>THE TRADE · 一句话说清这笔买卖</div>")
    P.append(f"<div style='font-size:16px;line-height:1.75'>"
             f"<b>{currency} {pick.entry_price:.2f} 附近买 {pick.ticker}"
             + (f"（{nm_tk}）" if nm_tk else "")
             + f"</b>，认错线 <b style='color:{RED}'>{currency} {pick.stop_price:.2f}</b>"
             f"——跌破就走，不商量；赚够两倍风险，认错线自动抬到成本价，"
             f"这用于降低回撤，不保证保本。</div>")
    bio = _TICKER_BIO.get(pick.ticker.upper(), "")
    if bio:
        P.append(f"<div class='sub' style='margin-top:4px'>它是谁：{_esc(bio)}</div>")
    cur = cand.price if (cand and cand.price) else None
    if cur:
        prem = (cur / pick.entry_price - 1.0) if pick.entry_price else 0.0
        price_html = (f"<div style='text-align:right;flex:none'><div class='sub'>日K收盘价</div>"
                      f"<div style='font-size:23px;font-weight:800;color:{GOLD}'>"
                       f"{currency} {cur:.2f}</div>"
                      f"<div class='sub'>较入场参考 {prem:+.1%}</div></div>")
    else:
        price_html = ("<div style='text-align:right;flex:none'><div class='sub'>日K收盘价</div>"
                      "<div style='font-size:23px;font-weight:800'>—</div>"
                      "<div class='sub'>本轮未取到</div></div>")
    name_zh = _name_zh(pick.ticker)
    name_span = (f"<span style='font-size:15px;color:{GOLD};font-weight:600;margin-left:2px'>"
                 f"{_esc(name_zh)}</span> ") if name_zh else ""
    P.append(f"<div style='display:flex;justify-content:space-between;align-items:center;gap:10px'>"
             f"<div><div style='font-size:19px'><b>{_esc(pick.ticker)}</b> {name_span}"
             f"<span class='tag' style='color:{GOLD};border-color:{GOLD}55'>"
             f"{_esc(_TEMPLATE_ZH.get(pick.entry_template, pick.entry_template or '标准建仓'))}</span></div>"
             f"<div class='sub' style='margin-top:2px'>{_esc(_etf_zh(pick.sector))} 板块"
             f" ｜ {_esc(_chain_zh(pick.chain))}</div></div>"
             f"{price_html}</div>")
    P.append(f"<div style='margin:8px 0 2px'>{_esc(concl)}</div>")
    if tech_hit:
        P.append(f"<div class='sub'>所属{_esc(_chain_zh(tech_hit['chain_id']))}景气上行"
                 f"（{_prosperity_label(tech_hit.get('prosperity'))}），"
                 "为该股评分提供额外支撑</div>")

    # —— 交易计划 KPI ——
    rank = next((i + 1 for i, p in enumerate(r.picks) if p.ticker == pick.ticker), None)
    P.append("<div class='grid g4' style='margin:12px 0'>"
             f"<div><div class='sub'>买点（计划买入区）</div><div class='kpi'>{pick.entry_price:.2f}</div></div>"
             f"<div><div class='sub'>认错线（跌破就走）</div><div class='kpi' style='color:{RED}'>"
             f"{pick.stop_price:.2f}</div><div class='sub'>{stop_pct:+.1f}%</div></div>"
             f"<div><div class='sub'>计划股数</div><div class='kpi'>{pick.shares}</div>"
             f"<div class='sub'>仓位 {pick.position_pct:.1%}</div></div>"
             f"<div><div class='sub'>计划风险</div><div class='kpi'>${pick.risk_usd:,.0f}</div>"
             + (f"<div class='sub'>今日优先级 #{rank}</div>" if rank else "") +
             "</div></div>"
             "<div class='sub'>跳空、滑点与佣金可能使实际亏损超过计划风险。</div>")

    # —— 价格阶梯图：止损 → 入场 → 2R 盈利保护，风险收益一图看清 ——
    ladder = _price_ladder_svg(pick)
    if ladder:
        P.append(f"<div style='margin:2px 0 10px'>{ladder}</div>")

    # —— 三关过闸明细（业务表述，不暴露内部阈值参数）——
    P.append("<table><tr><th>关卡</th><th>读数</th><th>业务标准</th><th>判定</th></tr>")
    if mrs:
        P.append(_gate_row("第一关 · 市场环境", f"{mrs.mrs_star}/10",
                           "达到允许进攻门槛", mrs.mrs_star >= 6.0))
    if sec:
        chain_hot = bool(chain and chain.hot)
        shs_ok = sec.in_main_pool or (chain_hot and sec.shs >= 7.0)
        how = "最强主线" if sec.in_main_pool else ("热区链通道" if shs_ok else "未达标准")
        P.append(_gate_row(f"第二关 · 板块质量（{_etf_zh(sec.etf)}）",
                           f"{sec.shs}/10（{how}）",
                           "主线板块或热区链支撑", shs_ok))
    P.append(_gate_row("第三关 · 个股质量", f"{pick.tss_final}/10",
                       "达到建仓质量标准", pick.tss_final >= 7.2))
    P.append("</table>")

    # —— 决策依据（五段式：算式/构成/证据全透传 + 证伪条件）——
    P.append("<details style='margin-top:14px;padding-top:10px;border-top:1px dashed " + BORDER + "'>"
             "<summary><b>看判卷过程</b>（五段业务依据，逐项可审计，含失效与离场条件）</summary>"
             "<div style='margin-top:8px'>")
    P.append(_rationale_detail(r, pick, cand, sec, chain, tech_hit))
    P.append("</div></details>")

    # —— 原始交易卡片（风控纪律）——
    if pick.card:
        P.append(f"<details style='margin-top:10px'><summary>原始交易卡片"
                 f"（含证伪/时间止损/盈利保护纪律）</summary>"
                 f"<pre class='card-block' style='margin-top:8px'>{_esc(pick.card)}</pre></details>")
    P.append("</div>")
    return "".join(P)


# ---------------------------------------------------------------- 小虎模拟盘
_GITHUB_URL = "https://cnb.cool/workloom-ai/workroom-tiger"
# 公开验证首个统计显著性检查点（机制见 docs/PUBLIC_VERIFICATION.md）：
# 以累计真实样本重跑 WFA，用 DSR 判定策略有效性，结论照实公开
_DSR_CHECKPOINT = "2026-10-30"


def _equity_svg(curve: list[dict], initial: float | None, width: int = 720,
                height: int = 180) -> str:
    """净值曲线；只有已记录的初始资金才能作为资金基线。"""
    initial = _number(initial)
    values = [_number(e.get("equity")) for e in curve]
    pts_v = ([initial] if initial is not None else []) + [v for v in values if v is not None]
    if len(pts_v) < 2:
        return ""
    lo, hi = min(pts_v), max(pts_v)
    pad = (hi - lo) * 0.08 or abs(pts_v[-1]) * 0.01 or 1.0
    lo, hi = lo - pad, hi + pad
    span = (hi - lo) or 1.0
    step = width / max(1, len(pts_v) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 16 - (v - lo) / span * (height - 34):.1f}"
                   for i, v in enumerate(pts_v))
    last = pts_v[-1]
    color = "#ffd700" if last >= pts_v[0] else "#ff6b6b"
    baseline = ""
    if initial is not None:
        base_y = height - 16 - (initial - lo) / span * (height - 34)
        baseline = (f'<line x1="10" y1="{base_y:.1f}" x2="{width - 10}" y2="{base_y:.1f}" '
                    f'stroke="#cbb26a" stroke-opacity="0.7" stroke-dasharray="5 4"/>'
                    f'<text x="{width - 14}" y="{base_y - 5:.1f}" fill="#cbb26a" font-size="10" '
                    f'text-anchor="end">初始 ${initial:,.0f}</text>')
    lx, ly = (len(pts_v) - 1) * step, height - 16 - (last - lo) / span * (height - 34)
    area = f"0,{height - 14} {pts} {lx:.1f},{height - 14}"
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">'
            f'<defs><linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="#ffd700" stop-opacity="0.32"/>'
            f'<stop offset="100%" stop-color="#ffd700" stop-opacity="0.02"/>'
            f'</linearGradient></defs>'
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" '
            f'fill="#0e0e11" stroke="#d4af37" stroke-opacity="0.5"/>'
            f'{baseline}'
            f'<polygon points="{area}" fill="url(#eg)"/>'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.6" '
            f'stroke-dasharray="2600" stroke-dashoffset="2600">'
            f'<animate attributeName="stroke-dashoffset" from="2600" to="0" dur="1.6s" '
            f'fill="freeze" calcMode="spline" keySplines="0.4 0 0.2 1"/></polyline>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{color}"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="none" stroke="{color}" '
            f'stroke-width="1.5"><animate attributeName="r" values="4.5;12" dur="1.4s" '
            f'repeatCount="indefinite"/><animate attributeName="stroke-opacity" '
            f'values="0.9;0" dur="1.4s" repeatCount="indefinite"/></circle>'
            f'<text x="{width - 14}" y="18" fill="{color}" font-size="13" font-weight="800" '
            f'text-anchor="end">${last:,.0f}</text></svg>')


def _avatar_svg(size: int = 64) -> str:
    """小虎 化身（内嵌 SVG 简笔，无外链）。"""
    return (f'<svg viewBox="0 0 64 64" width="{size}" height="{size}">'
            f'<circle cx="32" cy="32" r="30" fill="#e8f5c8" stroke="{GOLD}" stroke-width="2"/>'
            f'<circle cx="32" cy="26" r="10" fill="{GOLD}"/>'
            f'<path d="M14 52 Q32 36 50 52" fill="{GOLD}"/>'
            f'<circle cx="28" cy="24" r="1.6" fill="#1c2a10"/>'
            f'<circle cx="36" cy="24" r="1.6" fill="#1c2a10"/>'
            f'<text x="32" y="60" font-size="8" fill="{GOLD}" text-anchor="middle">G</text></svg>')


def _hero_svg(overlay: bool = False) -> str:
    """Hero 背景装饰。overlay=True 时透明底（叠在整站猛虎大背景之上），只画网格/光线/金粉。"""
    grid = []
    # 金色透视网格：竖线自远方消失点发散（仅完整模式；叠图模式会扫过猛虎面部，禁用）
    vp_x, vp_y = 620, 128
    for bx in range(-200, 1500, 120):
        grid.append(f"<line x1='{vp_x}' y1='{vp_y}' x2='{bx}' y2='360' "
                    f"stroke='#d4af37' stroke-opacity='0.28' stroke-width='1'/>")
    grid_h = []
    for i, y in enumerate([142, 164, 192, 226, 268, 318]):
        w = 1.5 - i * 0.16
        grid_h.append(f"<line x1='0' y1='{y}' x2='1200' y2='{y}' "
                      f"stroke='#d4af37' stroke-opacity='{0.32 - i * 0.04:.2f}' "
                      f"stroke-width='{max(w, 0.6):.1f}'/>")
    grid += grid_h
    # 金色上升 K 线（左下 → 右上，阳线亮金实心，阴线暗灰描金边）
    ohlc = [(36, 120, 96, 108), (92, 112, 84, 92), (148, 96, 72, 80),
            (204, 84, 64, 72), (260, 76, 54, 60), (316, 62, 46, 52),
            (372, 56, 36, 44), (428, 44, 30, 36), (484, 40, 22, 28),
            (540, 30, 16, 22), (596, 24, 10, 16), (652, 16, 4, 10)]
    candles = []
    for i, (x, o, c, h) in enumerate(ohlc):
        up = c < o
        top, bot = min(o, c), max(o, c)
        col = "#ffd700" if up else "#4b4b52"
        candles.append(f"<line x1='{x + 9}' y1='{h}' x2='{x + 9}' y2='{bot + 18}' "
                       f"stroke='{'#f5c542' if up else '#6b6b74'}' stroke-width='2.4' "
                       f"stroke-opacity='0.9'/>")
        if up:
            candles.append(f"<rect x='{x}' y='{top}' width='18' height='{max(bot - top, 4)}' "
                           f"rx='2.5' fill='{col}' fill-opacity='0.95'/>")
        else:
            candles.append(f"<rect x='{x}' y='{top}' width='18' height='{max(bot - top, 4)}' "
                           f"rx='2.5' fill='{col}' fill-opacity='0.85' "
                           f"stroke='#d4af37' stroke-opacity='0.6' stroke-width='1'/>")
    # 金色神经网络（右侧 AI 大脑区，带光晕节点）
    nodes = [(880, 56), (962, 30), (1050, 62), (1128, 28), (928, 116), (1012, 102),
             (1100, 124), (896, 182), (986, 172), (1076, 192), (1146, 160)]
    links = [(0, 1), (1, 2), (2, 3), (0, 4), (1, 5), (2, 5), (3, 6), (4, 5), (5, 6),
             (4, 7), (5, 8), (6, 9), (7, 8), (8, 9), (6, 10), (9, 10), (1, 4), (2, 6)]
    net = [f"<line x1='{nodes[a][0]}' y1='{nodes[a][1]}' x2='{nodes[b][0]}' y2='{nodes[b][1]}' "
           f"stroke='#d4af37' stroke-opacity='0.55' stroke-width='1.3'/>" for a, b in links]
    net += [f"<circle cx='{x}' cy='{y}' r='{16 if i % 3 == 0 else 12}' fill='#ffd700' "
            f"fill-opacity='0.14'/>"
            f"<circle cx='{x}' cy='{y}' r='{5.5 if i % 3 else 7}' fill='#ffd700'/>"
            f"<circle cx='{x}' cy='{y}' r='{10 if i % 3 else 13}' fill='none' "
            f"stroke='#ffe27a' stroke-opacity='0.75' stroke-width='1.3'/>"
            for i, (x, y) in enumerate(nodes)]
    # 金色数据流光线（三条穿越画面的上升轨迹）
    flow = ("<path d='M-20 286 C 260 256, 420 204, 640 162 S 1020 66, 1240 22' "
            "fill='none' stroke='#ffd700' stroke-width='3' stroke-opacity='0.85'/>"
            "<path d='M-20 306 C 300 278, 480 230, 700 190 S 1050 100, 1240 52' "
            "fill='none' stroke='#d4af37' stroke-width='1.8' stroke-opacity='0.55'/>"
            "<path d='M-20 266 C 240 240, 460 188, 680 146 S 1000 52, 1240 8' "
            "fill='none' stroke='#fff7d6' stroke-width='1' stroke-opacity='0.4'/>")
    # 散落金粉粒子
    import random as _rnd
    _r = _rnd.Random(42)
    dust = "".join(f"<circle cx='{_r.randint(20, 1180)}' cy='{_r.randint(8, 320)}' "
                   f"r='{_r.uniform(0.8, 2.2):.1f}' fill='#ffd700' "
                   f"fill-opacity='{_r.uniform(0.25, 0.7):.2f}'/>" for _ in range(46))
    if overlay:  # 叠图模式：透明底，仅横向网格线 + 金粉
        # （发散竖线与数据流光线会扫过猛虎面部，按设计要求移除）
        return ("<svg class='hero-bg' viewBox='0 0 1200 340' "
                "preserveAspectRatio='xMidYMid slice' aria-hidden='true'>"
                f"<g>{''.join(grid_h)}</g>{dust}</svg>")
    return ("<svg class='hero-bg' viewBox='0 0 1200 340' "
            "preserveAspectRatio='xMidYMid slice' aria-hidden='true'>"
            "<defs>"
            "<linearGradient id='hbk' x1='0' y1='0' x2='1' y2='1'>"
            "<stop offset='0%' stop-color='#1a1a10'/>"
            "<stop offset='45%' stop-color='#0e0e0c'/>"
            "<stop offset='100%' stop-color='#08080a'/>"
            "</linearGradient>"
            "<radialGradient id='hg' cx='80%' cy='18%' r='70%'>"
            "<stop offset='0%' stop-color='#ffd700' stop-opacity='0.34'/>"
            "<stop offset='50%' stop-color='#ffd700' stop-opacity='0.1'/>"
            "<stop offset='100%' stop-color='#ffd700' stop-opacity='0'/>"
            "</radialGradient>"
            "<radialGradient id='hg2' cx='12%' cy='88%' r='55%'>"
            "<stop offset='0%' stop-color='#d4af37' stop-opacity='0.2'/>"
            "<stop offset='100%' stop-color='#d4af37' stop-opacity='0'/>"
            "</radialGradient>"
            "<linearGradient id='hf' x1='0' y1='0' x2='0' y2='1'>"
            "<stop offset='0%' stop-color='#08080a' stop-opacity='0'/>"
            "<stop offset='78%' stop-color='#08080a' stop-opacity='0.25'/>"
            "<stop offset='100%' stop-color='#08080a' stop-opacity='0.72'/>"
            "</linearGradient></defs>"
            "<rect width='1200' height='340' fill='url(#hbk)'/>"
            f"<rect width='1200' height='340' fill='url(#hg)'/>"
            f"<rect width='1200' height='340' fill='url(#hg2)'/>"
            f"<g>{''.join(grid)}</g><g>{''.join(candles)}</g>{flow}{dust}"
            f"<g>{''.join(net)}</g>"
            "<rect width='1200' height='340' fill='url(#hf)'/></svg>")


def _pulse_svg(width: int = 216, height: int = 76) -> str:
    """决策门面卡·行情脉搏图（黑金面板：深黑底 + 金色 sparkline + 末端脉冲点）。"""
    pts_v = [22, 26, 21, 30, 27, 36, 31, 42, 38, 47, 44, 40, 48, 45, 54, 51, 58]
    step = (width - 32) / (len(pts_v) - 1)
    pts = " ".join(f"{16 + i * step:.1f},{height - 16 - v / 58 * (height - 34):.1f}"
                   for i, v in enumerate(pts_v))
    lx, ly = 16 + (len(pts_v) - 1) * step, height - 16 - pts_v[-1] / 58 * (height - 34)
    return (f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' "
            f"aria-hidden='true'><defs><linearGradient id='pg' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0%' stop-color='#ffd700' stop-opacity='0.5'/>"
            f"<stop offset='100%' stop-color='#ffd700' stop-opacity='0.03'/>"
            f"</linearGradient></defs>"
            f"<rect x='0.5' y='0.5' width='{width - 1}' height='{height - 1}' rx='12' "
            f"fill='#0e0e11' stroke='#d4af37' stroke-opacity='0.5'/>"
            f"<polygon points='16,{height - 14} {pts} {lx:.1f},{height - 14}' fill='url(#pg)'/>"
            f"<polyline points='{pts}' fill='none' stroke='#ffd700' stroke-width='2.6'/>"
            f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r='4.5' fill='#ffd700'/>"
            f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r='9' fill='none' stroke='#ffe27a' "
            f"stroke-width='1.6' stroke-opacity='0.9'/>"
            f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r='13.5' fill='none' stroke='#ffd700' "
            f"stroke-width='1' stroke-opacity='0.4'/></svg>")


def _resonance_svg() -> str:
    """理念页·四层共振倒金字塔（黑金面板：深黑底 + 四层金色阶梯，逐层收窄聚焦）。"""
    layers = [("市场环境 · 交易许可", "开不开车：定总仓位上限", "#ffe27a", 130, 1070),
              ("板块主线 · 资金方向", "走哪条路：主线池 1–2 条", "#ffd700", 240, 960),
              ("产业链周期 · 利润传导", "哪段效率最高：热区加成", "#e6b400", 350, 850),
              ("个股结构 · 建仓质量", "值不值做：R 反推股数", "#c28e00", 460, 740)]
    rows = []
    for i, (name, desc, col, x0, x1) in enumerate(layers):
        y = 26 + i * 62
        inset = 40
        nx0, nx1 = x0 + inset, x1 - inset
        rows.append(f"<polygon points='{x0},{y} {x1},{y} {nx1},{y + 52} {nx0},{y + 52}' "
                    f"fill='{col}'/>"
                    f"<text x='600' y='{y + 24}' text-anchor='middle' fill='#241a02' "
                    f"font-size='17' font-weight='800'>{name}</text>"
                    f"<text x='600' y='{y + 43}' text-anchor='middle' fill='#4a3603' "
                    f"font-size='12.5' font-weight='600'>{desc}</text>")
        if i < 3:
            rows.append(f"<polygon points='586,{y + 54} 614,{y + 54} 600,{y + 66}' "
                        f"fill='#ffd700'/>")
    return ("<svg viewBox='0 0 1200 292' width='100%' aria-hidden='true'>"
            "<rect x='1' y='1' width='1198' height='290' rx='14' fill='#0e0e11' "
            "stroke='#d4af37' stroke-opacity='0.55'/>"
            + "".join(rows) + "</svg>")


def _pipeline_svg() -> str:
    """理念页·六层决策流水线（黑金面板：金色节点 + 虚线金链）。"""
    steps = [("股票池扫描", "本轮池 → 候选"), ("市场许可", "仓位上限"),
             ("主线识别", "1–2 条主线"), ("产业链", "热区/衰退"),
             ("建仓质量", "模板+锚点"), ("风控闸门", "五态行动")]
    parts = []
    for i, (name, sub) in enumerate(steps):
        x = 96 + i * 202
        parts.append(f"<circle cx='{x}' cy='40' r='30' fill='#ffd700' fill-opacity='0.16'/>"
                     f"<circle cx='{x}' cy='40' r='22' fill='#ffd700' stroke='#ffe27a' "
                     f"stroke-width='2'/>"
                     f"<text x='{x}' y='46' text-anchor='middle' font-size='16' "
                     f"font-weight='800' fill='#241a02'>{i}</text>"
                     f"<text x='{x}' y='86' text-anchor='middle' font-size='13.5' "
                     f"font-weight='700' fill='#f5f2e4'>{name}</text>"
                     f"<text x='{x}' y='104' text-anchor='middle' font-size='11' "
                     f"fill='#cbb26a'>{sub}</text>")
        if i < 5:
            parts.append(f"<line x1='{x + 27}' y1='40' x2='{x + 175}' y2='40' "
                         f"stroke='#d4af37' stroke-width='2.5' stroke-dasharray='2 6' "
                         f"stroke-linecap='round'/>"
                         f"<polygon points='{x + 167},{35} {x + 181},{40} {x + 167},{45}' "
                         f"fill='#ffd700'/>")
    return ("<svg viewBox='0 0 1200 118' width='100%' aria-hidden='true'>"
            "<rect x='1' y='1' width='1198' height='116' rx='14' fill='#0e0e11' "
            "stroke='#d4af37' stroke-opacity='0.55'/>"
            + "".join(parts) + "</svg>")


def _ticker_badge_svg(ticker: str, size: int = 76, market: str = "STOCK") -> str:
    """个股·黑金徽章（公司 logo 位：外金环 + 电路刻度 + 代码金字）。"""
    t = html.escape(ticker)
    ticks = "".join(
        f"<line x1='60' y1='4' x2='60' y2='12' stroke='#d4af37' stroke-width='2' "
        f"stroke-opacity='0.8' transform='rotate({a} 60 60)'/>"
        for a in range(0, 360, 30))
    fs = 30 if len(t) <= 4 else (24 if len(t) <= 5 else 19)
    return (f"<svg viewBox='0 0 120 120' width='{size}' height='{size}' aria-hidden='true'>"
            f"<defs><radialGradient id='tb' cx='35%' cy='30%' r='85%'>"
            f"<stop offset='0%' stop-color='#2b2b22'/>"
            f"<stop offset='100%' stop-color='#0c0c0e'/></radialGradient></defs>"
            f"<circle cx='60' cy='60' r='56' fill='url(#tb)' stroke='#ffd700' "
            f"stroke-width='2.5'/>{ticks}"
            f"<circle cx='60' cy='60' r='45' fill='none' stroke='#d4af37' "
            f"stroke-opacity='0.55' stroke-width='1' stroke-dasharray='3 4'/>"
            f"<text x='60' y='{64 + fs * 0.12:.0f}' text-anchor='middle' font-size='{fs}' "
            f"font-weight='800' fill='#ffd700' letter-spacing='1'>{t}</text>"
            f"<text x='60' y='88' text-anchor='middle' font-size='9' fill='#cbb26a' "
            f"letter-spacing='2.5'>{_esc(market)}</text></svg>")


_CHAIN_THEME = {"memory": "memory", "logic": "logic", "foundry": "foundry",
                "equipment": "equipment", "ai_model": "ai_model", "ai_app": "ai_app"}


def _theme_art_html(chain_id: str, width: int = 128) -> str:
    """产业链主题产品图（AI 生成内嵌 base64）；无对应主题时回退通用金牛图。"""
    key = _CHAIN_THEME.get(chain_id or "", "tech")
    b64 = THEME_ART_B64.get(key) or THEME_ART_B64.get("tech", "")
    if not b64:
        return ""
    label = _chain_zh(chain_id) if chain_id else "科技主线"
    return (f"<div style='flex:0 0 auto;text-align:center'>"
            f"<img src='data:image/jpeg;base64,{b64}' width='{width}' height='{width}' "
            f"alt='{_esc(label)}主题产品图' style='border-radius:14px;"
            f"border:1.5px solid #d4af37;box-shadow:0 4px 22px #d4af3745;display:block'/>"
            f"<div class='sub' style='margin-top:5px;font-size:10.5px'>AI 生成 · "
            f"{_esc(label)}主题</div></div>")


def _coin_rain_svg(width: int = 1200, height: int = 92) -> str:
    """模拟盘·美金金币雨横幅（SMIL 循环动画，内嵌 SVG，零外链）。"""
    import random as _rnd
    r = _rnd.Random(7)
    coins = []
    for i in range(14):
        x = 36 + i * 84 + r.randint(-16, 16)
        dur = r.uniform(2.8, 4.8)
        begin = -r.uniform(0, 4.5)
        size = r.choice([10, 12, 14, 16])
        coins.append(
            f"<g opacity='0'>"
            f"<circle cx='0' cy='0' r='{size}' fill='#ffd700' stroke='#b8860b' "
            f"stroke-width='1.5'/>"
            f"<text x='0' y='{size * 0.36:.1f}' text-anchor='middle' "
            f"font-size='{size:.0f}' font-weight='800' fill='#241a02'>$</text>"
            f"<animateTransform attributeName='transform' type='translate' "
            f"values='{x} -26; {x} {height + 30}' dur='{dur:.1f}s' begin='{begin:.1f}s' "
            f"repeatCount='indefinite'/>"
            f"<animate attributeName='opacity' values='0;0.9;0.9;0' "
            f"keyTimes='0;0.14;0.82;1' dur='{dur:.1f}s' begin='{begin:.1f}s' "
            f"repeatCount='indefinite'/></g>")
    return (f"<svg viewBox='0 0 {width} {height}' width='100%' aria-hidden='true' "
            f"style='display:block'>"
            f"<rect width='{width}' height='{height}' rx='12' fill='#0e0e11'/>"
            f"<rect x='0.5' y='0.5' width='{width - 1}' height='{height - 1}' rx='12' "
            f"fill='none' stroke='#d4af37' stroke-opacity='0.55'/>"
            + "".join(coins) +
            f"<text x='{width / 2}' y='{height / 2 + 7}' text-anchor='middle' "
            f"font-size='18' font-weight='800' fill='#ffd700' letter-spacing='5'>"
            f"小虎纯AI模拟盘 · 虚拟资金 · 全程留痕</text></svg>")


def _topology_svg() -> str:
    """首页底部·老虎全球资产管理拓扑图（六源情报 → 语义清洗 → 四层共振核心 → 风控闸门 → 三账输出）。

    黑金面板 + 中枢光环 + 贝塞尔金链 + 漂浮金粉，仅展示流程结构（不含任何公式参数）。
    """
    W, H = 1200, 620
    P: list[str] = []
    # —— 左侧：六源情报节点 ——
    sources = ["新闻情报", "券商研报", "社交舆情", "行情数据", "宏观利率", "公司公告"]
    for i, name in enumerate(sources):
        y = 66 + i * 84
        P.append(f"<rect x='52' y='{y}' width='176' height='50' rx='10' fill='#15151a' "
                 f"stroke='#d4af37' stroke-opacity='0.55'/>"
                 f"<circle cx='76' cy='{y + 25}' r='5' fill='#ffd700'/>"
                 f"<circle cx='76' cy='{y + 25}' r='9' fill='none' stroke='#ffd700' "
                 f"stroke-opacity='0.4'/>"
                 f"<text x='96' y='{y + 30}' font-size='15' font-weight='700' "
                 f"fill='#f5f2e4'>{name}</text>")
        # 汇聚曲线 → 语义清洗
        P.append(f"<path d='M228 {y + 25} C 330 {y + 25}, 330 310, 428 310' "
                 f"fill='none' stroke='#d4af37' stroke-opacity='0.5' stroke-width='1.6'/>")
    # —— 中左：AI 语义清洗 ——
    P.append("<rect x='428' y='272' width='150' height='76' rx='12' fill='#1b1b12' "
             "stroke='#ffd700' stroke-width='1.6'/>"
             "<text x='503' y='306' text-anchor='middle' font-size='15.5' "
             "font-weight='800' fill='#ffd700'>AI 语义清洗</text>"
             "<text x='503' y='328' text-anchor='middle' font-size='11.5' "
             "fill='#cbb26a'>LLM · 消歧/情感/叙事</text>")
    # —— 清洗 → 核心 ——
    P.append("<path d='M578 310 C 600 310, 598 310, 614 310' fill='none' "
             "stroke='#ffd700' stroke-width='2.4'/>"
             "<polygon points='608,305 620,310 608,315' fill='#ffd700'/>")
    # —— 中央：决策核心（双环 + 四卫星）——
    cx, cy = 726, 310
    sats = [("市场环境", -90), ("板块主线", 0), ("产业链", 90), ("个股结构", 180)]
    P.append(f"<circle cx='{cx}' cy='{cy}' r='104' fill='none' stroke='#d4af37' "
             f"stroke-opacity='0.35' stroke-width='1' stroke-dasharray='4 6'/>"
             f"<circle cx='{cx}' cy='{cy}' r='78' fill='#15130a' stroke='#ffd700' "
             f"stroke-width='2.5'/>"
             f"<circle cx='{cx}' cy='{cy}' r='64' fill='none' stroke='#ffd700' "
             f"stroke-opacity='0.4' stroke-width='1'/>"
             f"<text x='{cx}' y='{cy - 12}' text-anchor='middle' font-size='19' "
             f"font-weight='800' fill='#ffd700'>老虎全球资产管理</text>"
             f"<text x='{cx}' y='{cy + 12}' text-anchor='middle' font-size='15' "
             f"font-weight='700' fill='#ffe27a'>决策核心</text>"
             f"<text x='{cx}' y='{cy + 34}' text-anchor='middle' font-size='11' "
             f"fill='#cbb26a'>四层共振引擎</text>")
    for name, ang in sats:
        import math as _m
        sx = cx + 104 * _m.cos(_m.radians(ang))
        sy = cy + 104 * _m.sin(_m.radians(ang))
        P.append(f"<circle cx='{sx:.0f}' cy='{sy:.0f}' r='17' fill='#ffd700'/>"
                 f"<circle cx='{sx:.0f}' cy='{sy:.0f}' r='23' fill='none' "
                 f"stroke='#ffe27a' stroke-opacity='0.6' stroke-width='1.2'/>"
                 f"<text x='{sx:.0f}' y='{sy:.0f}' text-anchor='middle' "
                 f"font-size='10' fill='#241a02' font-weight='800' dy='.35em'>"
                 f"{name[:2]}</text>"
                 f"<text x='{sx:.0f}' y='{sy + 38:.0f}' text-anchor='middle' "
                 f"font-size='12' fill='#f5f2e4' font-weight='700'>{name}</text>")
    # —— 核心 → 风控闸门 ——
    P.append("<path d='M834 310 C 860 310, 858 310, 876 310' fill='none' "
             "stroke='#ffd700' stroke-width='2.4'/>"
             "<polygon points='870,305 882,310 870,315' fill='#ffd700'/>")
    P.append("<rect x='882' y='272' width='140' height='76' rx='12' fill='#1b1b12' "
             "stroke='#ffd700' stroke-width='1.6'/>"
             "<text x='952' y='306' text-anchor='middle' font-size='15.5' "
             "font-weight='800' fill='#ffd700'>风控闸门</text>"
             "<text x='952' y='328' text-anchor='middle' font-size='11.5' "
             "fill='#cbb26a'>五态行动 · R 反推</text>")
    # —— 右侧：三账输出 ——
    outs = [("📈 决策日报", 100), ("🤖 小虎模拟盘", 310), ("🗂 历史回溯", 520)]
    for name, y in outs:
        P.append(f"<path d='M1022 310 C 1056 310, 1042 {y + 26}, 1064 {y + 26}' "
                 f"fill='none' stroke='#d4af37' stroke-opacity='0.6' stroke-width='1.8'/>"
                 f"<rect x='1064' y='{y}' width='126' height='52' rx='10' fill='#15151a' "
                 f"stroke='#d4af37' stroke-opacity='0.6'/>"
                 f"<text x='1127' y='{y + 32}' text-anchor='middle' font-size='13.5' "
                 f"font-weight='700' fill='#f5f2e4'>{name}</text>")
    # —— 金粉粒子 ——
    import random as _rnd
    r = _rnd.Random(11)
    P.append("".join(f"<circle cx='{r.randint(30, 1170)}' cy='{r.randint(24, 596)}' "
                     f"r='{r.uniform(0.8, 1.9):.1f}' fill='#ffd700' "
                     f"fill-opacity='{r.uniform(0.2, 0.55):.2f}'/>" for _ in range(34)))
    # —— 底部铭文 ——
    P.append(f"<text x='{W / 2}' y='{H - 16}' text-anchor='middle' font-size='12.5' "
             f"fill='#cbb26a' letter-spacing='2'>六源情报 → 语义清洗 → 四层共振 → "
             f"风控闸门 → 三账输出 ｜ 全程留痕 · 逐项可审计</text>")
    return (f"<svg viewBox='0 0 {W} {H}' width='100%' aria-hidden='true' "
            f"style='display:block'><rect width='{W}' height='{H}' rx='16' "
            f"fill='#0c0c0f'/><rect x='0.5' y='0.5' width='{W - 1}' height='{H - 1}' "
            f"rx='16' fill='none' stroke='#d4af37' stroke-opacity='0.55'/>"
            + "".join(P) + "</svg>")


def _price_ladder_svg(pick, width: int = 1080, height: int = 108) -> str:
    """决策卡·价格阶梯图：止损（红）— 入场（金）— 2R 盈利保护位（绿），风险/收益分区。"""
    entry, stop = pick.entry_price, pick.stop_price
    if not entry or not stop or entry <= stop:
        return ""
    r1 = entry - stop
    t2 = entry + 2 * r1                      # 2R 盈利保护参考位
    lo, hi = stop - r1 * 0.7, t2 + r1 * 0.7
    span = hi - lo
    def X(v):
        return 60 + (v - lo) / span * (width - 120)
    xe, xs, xt = X(entry), X(stop), X(t2)
    y = 46
    return (f"<svg viewBox='0 0 {width} {height}' width='100%' aria-hidden='true'>"
            f"<defs><linearGradient id='lr' x1='0' y1='0' x2='1' y2='0'>"
            f"<stop offset='0%' stop-color='#dc2626' stop-opacity='0.28'/>"
            f"<stop offset='100%' stop-color='#dc2626' stop-opacity='0.05'/>"
            f"</linearGradient><linearGradient id='lg' x1='0' y1='0' x2='1' y2='0'>"
            f"<stop offset='0%' stop-color='#16a34a' stop-opacity='0.06'/>"
            f"<stop offset='100%' stop-color='#16a34a' stop-opacity='0.3'/>"
            f"</linearGradient></defs>"
            f"<rect x='{xs:.0f}' y='{y - 16}' width='{xe - xs:.0f}' height='32' "
            f"fill='url(#lr)'/>"
            f"<rect x='{xe:.0f}' y='{y - 16}' width='{xt - xe:.0f}' height='32' "
            f"fill='url(#lg)'/>"
            f"<line x1='{xs:.0f}' y1='{y}' x2='{xt:.0f}' y2='{y}' stroke='#d4af37' "
            f"stroke-width='2.5'/>"
            # 止损标记
            f"<line x1='{xs:.0f}' y1='{y - 22}' x2='{xs:.0f}' y2='{y + 22}' "
            f"stroke='#dc2626' stroke-width='3'/>"
            f"<text x='{xs:.0f}' y='{y - 30}' text-anchor='middle' font-size='13' "
            f"font-weight='800' fill='#dc2626'>止损 {stop:.2f}</text>"
            f"<text x='{xs:.0f}' y='{y + 38}' text-anchor='middle' font-size='11' "
            f"fill='#dc2626'>风险 1R</text>"
            # 入场标记（菱形）
            f"<polygon points='{xe:.0f},{y - 26} {xe + 9:.0f},{y - 14} {xe:.0f},{y - 2} "
            f"{xe - 9:.0f},{y - 14}' fill='#ffd700' stroke='#b8860b'/>"
            f"<text x='{xe:.0f}' y='{y - 34}' text-anchor='middle' font-size='13.5' "
            f"font-weight='800' fill='#8a6d00'>入场 {entry:.2f}</text>"
            f"<text x='{xe:.0f}' y='{y + 38}' text-anchor='middle' font-size='11' "
            f"fill='#8a6d00'>计划买入区</text>"
            # 2R 标记
            f"<line x1='{xt:.0f}' y1='{y - 22}' x2='{xt:.0f}' y2='{y + 22}' "
            f"stroke='#16a34a' stroke-width='3' stroke-dasharray='6 4'/>"
            f"<text x='{xt:.0f}' y='{y - 30}' text-anchor='middle' font-size='13' "
            f"font-weight='800' fill='#16a34a'>2R 保护 {t2:.2f}</text>"
            f"<text x='{xt:.0f}' y='{y + 38}' text-anchor='middle' font-size='11' "
            f"fill='#16a34a'>浮盈到此止损上移至成本线</text></svg>")


def _cycle_svg(size: int = 210) -> str:
    """产业链景气循环图：复苏 → 扩张 → 过热 → 衰退 四阶段环形轮动。"""
    stages = [("复苏", "#84cc16", -90), ("扩张", "#ffd700", 0),
              ("过热", "#f59e0b", 90), ("衰退", "#9ca3af", 180)]
    import math as _m
    cx = cy = size / 2
    R = size * 0.36
    P = [f"<circle cx='{cx}' cy='{cy}' r='{R}' fill='none' stroke='#d4af37' "
         f"stroke-width='2' stroke-dasharray='5 5'/>"
         f"<text x='{cx}' y='{cy - 4}' text-anchor='middle' font-size='15' "
         f"font-weight='800' fill='#8a6d00'>景气循环</text>"
         f"<text x='{cx}' y='{cy + 16}' text-anchor='middle' font-size='10.5' "
         f"fill='#5f7050'>阶段轮动 · 热区加成</text>"]
    for name, col, ang in stages:
        a = _m.radians(ang)
        x, y = cx + R * _m.cos(a), cy + R * _m.sin(a)
        P.append(f"<circle cx='{x:.0f}' cy='{y:.0f}' r='24' fill='{col}'/>"
                 f"<circle cx='{x:.0f}' cy='{y:.0f}' r='29' fill='none' "
                 f"stroke='{col}' stroke-opacity='0.45' stroke-width='1.5'/>"
                 f"<text x='{x:.0f}' y='{y:.0f}' text-anchor='middle' dy='.35em' "
                 f"font-size='13.5' font-weight='800' fill='#241a02'>{name}</text>")
        # 顺时针箭头（阶段间 45° 处）
        a2 = _m.radians(ang + 45)
        ax, ay = cx + R * _m.cos(a2), cy + R * _m.sin(a2)
        a3 = _m.radians(ang + 45 + 90)
        P.append(f"<polygon points='0,-5 10,0 0,5' fill='#d4af37' "
                 f"transform='translate({ax:.0f},{ay:.0f}) rotate({ang + 45 + 90})'/>")
    return (f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}' "
            f"aria-hidden='true'>{''.join(P)}</svg>")


def _gauges_row_svg(dims: dict, width: int = 1080) -> str:
    """市场环境五维仪表盘（黑金圆环仪表 + 评分 + 状态灯）。"""
    keys = ["macro", "flow", "sent", "tech", "micro"]
    n = len(keys)
    cell = width / n
    R = 44
    P = []
    for i, k in enumerate(keys):
        d = dims.get(k)
        sc = _number(_get(d, "score")) if d else None
        cx = cell * i + cell / 2
        cy = 78
        if sc is None:
            col, frac, txt = "#9ca3af", 0.0, "—"
        else:
            frac = max(0.0, min(1.0, sc / 10.0))
            col = "#16a34a" if sc >= 6.5 else ("#ffd700" if sc >= 5 else "#dc2626")
            txt = f"{sc:.1f}"
        import math as _m
        circ = 2 * _m.pi * R
        P.append(f"<circle cx='{cx:.0f}' cy='{cy}' r='{R}' fill='#15151a' "
                 f"stroke='#2e2e36' stroke-width='7'/>"
                 f"<circle cx='{cx:.0f}' cy='{cy}' r='{R}' fill='none' stroke='{col}' "
                 f"stroke-width='7' stroke-linecap='round' "
                 f"stroke-dasharray='{frac * circ:.0f} {circ:.0f}' "
                 f"transform='rotate(-90 {cx:.0f} {cy})'/>"
                 f"<text x='{cx:.0f}' y='{cy + 6}' text-anchor='middle' font-size='21' "
                 f"font-weight='800' fill='#f5f2e4'>{txt}</text>"
                 f"<text x='{cx:.0f}' y='{cy + 76}' text-anchor='middle' font-size='14' "
                 f"font-weight='700' fill='#f5f2e4'>{_DIM_ZH.get(k, k)}</text>")
        if d is not None:
            P.append(f"<text x='{cx:.0f}' y='{cy + 95}' text-anchor='middle' "
                     f"font-size='11' fill='#cbb26a'>{_health(sc)}</text>")
    return (f"<svg viewBox='0 0 {width} 200' width='100%' aria-hidden='true'>"
            f"<rect width='{width}' height='200' rx='14' fill='#0e0e11'/>"
            f"<rect x='0.5' y='0.5' width='{width - 1}' height='199' rx='14' "
            f"fill='none' stroke='#d4af37' stroke-opacity='0.55'/>"
            + "".join(P) + "</svg>")


def _funnel_svg(total: int, passed: int, selected: int, n_pick: int,
                width: int = 1080) -> str:
    """今日选股漏斗图：本轮扫描池 → 门槛 → 精评 → 放行。"""
    layers = [("本轮扫描池", total, "#ffe27a", 60, 1020),
              ("通过基础门槛", passed, "#ffd700", 190, 890),
              ("进入深度精评", selected, "#e6b400", 320, 760),
              ("今日最终放行", n_pick, "#c28e00", 450, 630)]
    P = []
    for i, (name, v, col, x0, x1) in enumerate(layers):
        y = 18 + i * 66
        nx0, nx1 = x0 + 45, x1 - 45
        P.append(f"<polygon points='{x0},{y} {x1},{y} {nx1},{y + 54} {nx0},{y + 54}' "
                 f"fill='{col}'/>"
                 f"<text x='{(x0 + x1) / 2:.0f}' y='{y + 26}' text-anchor='middle' "
                 f"font-size='15.5' font-weight='800' fill='#241a02'>{name}</text>"
                 f"<text x='{(x0 + x1) / 2:.0f}' y='{y + 45}' text-anchor='middle' "
                 f"font-size='13' font-weight='700' fill='#4a3603'>{v} 只</text>")
        if i < 3 and layers[i + 1][1] is not None and v:
            drop = (1 - (layers[i + 1][1] or 0) / v) * 100
            P.append(f"<text x='{width - 30}' y='{y + 44}' text-anchor='end' "
                     f"font-size='12' font-weight='700' fill='#b45309'>"
                     f"↓ 淘汰 {drop:.0f}%</text>")
    return (f"<svg viewBox='0 0 {width} 286' width='100%' aria-hidden='true'>"
            + "".join(P) + "</svg>")


def _sim_tab(sim: dict) -> str:
    st, stats = sim["state"], sim["stats"]
    initial = _number(st.get("initial_cash"))
    initial_note = f"初始资金 ${initial:,.0f}" if initial is not None else "初始资金未知"
    P: list[str] = []
    # —— 醒目免责声明（公开传播纪律）——
    P.append(f"<div style='background:#fdeaea;border:2px solid {RED};border-radius:12px;"
             f"padding:14px 18px;margin-bottom:14px'>"
             f"<b style='color:{RED}'>⚠️ 全 AI 掌控的模拟盘（Paper Trading）</b>"
             f"<div style='margin-top:4px'>本页所有交易均由<b>老虎全球资产管理（Tiger Global Asset Management）</b>"
             f"自动决策与记账，{initial_note}；本页资金均为虚拟资金，目的是验证 AI 的投资能力。"
             f"<b>不构成任何投资建议或决策参考</b>，据此操作风险自负。"
             f"<div style='margin-top:4px'><b>口径披露：含保守摩擦成本口径"
             f"（滑点按ADV分档+单边10bp）</b>——成交额越小的标的滑点越大，"
             f"台账按毛/摩擦/净三栏记账，绝不用毛收益冒充净收益。</div></div></div>")
    P.append("<div class='eyebrow-l' style='margin-bottom:6px'>LIVE EXPERIMENT · 公开实验</div>"
             "<div style='font-size:15px;margin-bottom:12px;line-height:1.7'>"
             "<b>一个不自嗨的 AI：</b>用虚拟资金按真实规则交易，每一笔都公开记账，"
             "接受全世界审计。赚亏都挂在这儿，不删账、不粉饰。</div>")
    # —— 美金金币雨横幅（虚拟资金属性一眼即知）——
    P.append(f"<div style='margin-bottom:14px'>{_coin_rain_svg()}</div>")
    # —— 小虎 人设卡 + 资金看板 ——
    ret = _number(stats.get("cum_return")) if initial is not None else None
    ret_color = MUTED if ret is None else GREEN if ret >= 0 else RED
    ret_text = f"{ret:+.1%}" if ret is not None else "—"
    ret_note = "累计收益" if ret is not None else "累计收益未知（缺少初始资金记录）"
    P.append(f"<div class='card' style='border-color:{GOLD}66'><div style='display:flex;"
             f"gap:16px;align-items:center;flex-wrap:wrap'>"
             f"<div>{_avatar_svg()}</div>"
             f"<div style='flex:1;min-width:min(220px,100%)'>"
             f"<div style='font-size:18px'><b>小虎</b> <span class='sub'>老虎全球资产管理 AI 的人间化身"
             f"（全 AI 决策，零人工干预）</span>"
             f"<span class='tag' style='border-color:{GOLD};color:{GOLD}'>公开验证期</span></div>"
             f"<div class='sub'>{initial_note} ｜ 已记录 {stats['days']} 个净值日期 ｜ "
             f"成交规则：信号次日开盘价（无未来函数）</div></div>"
             f"<div style='text-align:right'>"
             f"<div class='kpi' style='color:{ret_color}'>{ret_text}</div>"
             f"<div class='sub'>{ret_note}</div></div></div>")
    P.append(f"<div class='grid g4' style='margin-top:12px'>"
             f"<div><div class='sub'>总净值</div><div class='kpi' id='simEquityKpi' "
             f"data-v='{stats['equity']:.0f}'>${stats['equity']:,.0f}</div></div>"
             f"<div><div class='sub'>现金 / 持仓市值</div><div class='kpi' style='font-size:20px'>"
             f"${stats['cash']:,.0f}</div><div class='sub'>${stats['invested']:,.0f}</div></div>"
             f"<div><div class='sub'>已结算交易</div><div class='kpi'>{stats['n_closed']}</div>"
             f"<div class='sub'>胜率 {stats['win_rate']:.0%}"
             f"{'' if stats['n_closed'] >= 100 else '（样本积累中，不作结论）'}</div></div>"
             f"<div><div class='sub'>最大回撤</div><div class='kpi' style='color:{YELLOW}'>"
             f"{stats['max_drawdown']:.1%}</div>"
             f"<div class='sub'>期望 {stats['expectancy_r']}R ｜ PF "
             f"{stats['profit_factor'] if stats['profit_factor'] is not None else '∞'}</div></div>"
             "</div>" if stats["n_closed"] else
             f"<div class='grid g4' style='margin-top:12px'>"
             f"<div><div class='sub'>总净值</div><div class='kpi' id='simEquityKpi' "
             f"data-v='{stats['equity']:.0f}'>${stats['equity']:,.0f}</div></div>"
             f"<div><div class='sub'>现金</div><div class='kpi' style='font-size:20px'>"
             f"${stats['cash']:,.0f}</div></div>"
             f"<div><div class='sub'>已结算交易</div><div class='kpi'>0</div>"
             f"<div class='sub'>尚无已结算交易</div></div>"
             f"<div><div class='sub'>最大回撤</div><div class='kpi'>{stats['max_drawdown']:.1%}</div>"
             f"<div class='sub'>逐日更新</div></div></div>")
    svg = _equity_svg(st["equity_curve"], initial)
    if svg:
        P.append(f"<div style='margin-top:10px'>{svg}</div>")
    P.append("</div>")
    # —— 毛 / 摩擦 / 净 三栏汇总（v6.3 保守摩擦口径，防滑点断崖）——
    if stats["n_closed"]:
        P.append(f"<div class='card' style='margin-top:12px'>"
                 f"<b>📒 三栏台账汇总</b> <span class='sub'>——恒等式：毛收益 − 摩擦成本 = 净收益"
                 f"（滑点按ADV分档+单边10bp佣金）</span>"
                 f"<div class='grid g3' style='margin-top:10px'>"
                 f"<div><div class='sub'>毛收益（无摩擦口径）</div>"
                 f"<div class='kpi' style='font-size:20px'>${stats.get('pnl_gross', 0):+,.0f}</div></div>"
                 f"<div><div class='sub'>摩擦成本（滑点+佣金）</div>"
                 f"<div class='kpi' style='font-size:20px;color:{YELLOW}'>−${stats.get('friction_total', 0):,.0f}</div></div>"
                 f"<div><div class='sub'>净收益（模拟盘记账）</div>"
                 f"<div class='kpi' style='font-size:20px;color:{GREEN if stats.get('pnl_net', 0) >= 0 else RED}'>"
                 f"${stats.get('pnl_net', 0):+,.0f}</div></div>"
                 f"</div></div>")
    # —— 计划风险边界（跳空与摩擦并不受计划止损价保证）——
    P.append(
        f"<div class='card' style='margin-top:12px'>"
        f"<b>🛡️ 风控边界</b> <span class='sub'>——按计划止损距离分配风险预算</span>"
        f"<div class='grid g3' style='margin-top:10px'>"
        f"<div><div class='kpi' style='font-size:20px;color:{GOLD}'>{config.RISK_R_PCT:.1%}</div>"
        f"<div class='sub'>单笔计划风险预算：股数由止损距离反推，实际预算还受当轮仓位与事件折扣约束</div></div>"
        f"<div><div class='kpi' style='font-size:20px;color:{GOLD}'>{config.MAX_SINGLE_POSITION_PCT:.0%}</div>"
        f"<div class='sub'>单票仓位上限：再看好也不超过净值 {config.MAX_SINGLE_POSITION_PCT:.0%}，杜绝一把梭</div></div>"
        f"<div><div class='kpi' style='font-size:20px;color:{GOLD}'>环境闸门</div>"
        f"<div class='sub'>市场环境评分跌破警戒线即禁止开新仓——空仓也是一种仓位</div></div>"
        f"<div><div class='kpi' style='font-size:20px;color:{GOLD}'>T+1</div>"
        f"<div class='sub'>信号次日开盘价成交，无未来函数；同日重跑不重复成交</div></div>"
        f"<div><div class='kpi' style='font-size:20px;color:{GOLD}'>5–7 日</div>"
        f"<div class='sub'>时间止损按信号设定的交易日数执行；旧记录缺少设置时使用默认 7 日</div></div>"
        f"<div><div class='kpi' style='font-size:20px;color:{GOLD}'>2R</div>"
        f"<div class='sub'>盈利保护：浮盈达两倍风险后止损上移至成本线，不保证保本</div></div>"
        f"</div><div class='sub' style='margin-top:8px'>"
        f"跳空、滑点与佣金可能使实际亏损超过计划风险。</div></div>")
    # —— 公开验证章程（定位与宣传纪律，详见 docs/PUBLIC_VERIFICATION.md）——
    P.append(
        f"<div class='card' style='margin-top:12px;border-color:{GOLD}55'>"
        f"<b>📜 公开验证章程</b>"
        f"<div style='margin-top:6px'>这不是荐股直播间，而是一场<b>全程留痕的公开实验</b>："
        f"一套 AI 系统能不能在真实市场里活下去，用可审计的方式验证给大家看。</div>"
        f"<ul style='margin:8px 0 0;padding-left:20px'>"
        f"<li><b>赚亏如实</b>：亏损交易日与亏损单同样公示，绝不粉饰、绝不删账。</li>"
        f"<li><b>不吹胜率</b>：累计成交未满 100 笔前，页面出现的胜率与期望值仅为过程快照，"
        f"不构成有效性结论——样本量不够时，沉默比宣传更专业。</li>"
        f"<li><b>可审计</b>：每日决策附完整业务依据与失效条件，行情来源血缘逐轮披露。</li>"
        f"<li><b>用统计说话</b>：{_DSR_CHECKPOINT} 前后以累计真实样本重跑滚动前推（WFA）检验，"
        f"用 Deflated Sharpe Ratio 判定策略有效性是否统计显著，结论照实公开。</li>"
        f"<li><b>边界</b>：虚拟资金，不构成投资建议；据此操作，风险自负。</li>"
        f"</ul></div>")
    # —— 今日操作 ——
    if st["ops_log"] and st["ops_log"][-1]["ops"]:
        today = st["ops_log"][-1]
        P.append(f"<div class='card' style='margin-top:12px'><b>今日操作</b>"
                 f"<span class='sub'>（{today['date']}）</span>")
        for op in today["ops"]:
            P.append(f"<div style='margin:3px 0'>{_esc(op)}</div>")
        P.append("</div>")
    # —— 当前持仓 ——
    P.append("<div class='card' style='margin-top:12px'><b>当前持仓</b>")
    if st["positions"]:
        P.append("<table style='margin-top:6px'><tr><th>标的</th><th>股数</th><th>成本</th>"
                 "<th>止损</th><th>初始计划风险</th><th>入场日</th><th>产业链</th></tr>")
        for p in st["positions"]:
            P.append(f"<tr><td><b>{p['ticker']}</b></td><td>{p['shares']}</td>"
                     f"<td>{p['entry_price']:.2f}</td><td style='color:{RED}'>{p['stop']:.2f}</td>"
                     f"<td>${p['risk_usd']:,.0f}</td><td>{p['entry_date']}</td>"
                     f"<td>{p.get('chain') or '-'}</td></tr>")
        P.append("</table>")
    else:
        P.append("<div class='sub' style='margin-top:4px'>当前无持仓"
                 + ("，有待成交信号。" if st["pending"] else "，也无待成交信号。") + "</div>")
    if st["pending"]:
        P.append(f"<div class='sub' style='margin-top:8px'>📋 待成交信号 "
                 f"{len(st['pending'])} 只（等待下一交易日有效开盘行情，满足成交条件后记账）："
                 f"{_esc('、'.join(p['ticker'] for p in st['pending']))}</div>")
    P.append("</div>")
    # —— 历史交易 ——
    P.append("<div class='card' style='margin-top:12px'><b>历史交易记录</b>"
             "<span class='sub'>（毛 / 摩擦 / 净 三栏台账：恒等式 毛−摩擦=净）</span>")
    if st["closed"]:
        P.append("<table style='margin-top:6px'><tr><th>标的</th><th>入场</th><th>出场</th>"
                 "<th>股数</th><th>盈亏(净)</th><th>毛R</th><th>摩擦</th><th>净R</th>"
                 "<th>天数</th><th>出场原因</th></tr>")
        for c in reversed(st["closed"][-50:]):
            col = GREEN if c["pnl_usd"] > 0 else RED
            gross_r = c.get("gross_r")
            friction = c.get("friction_cost")
            P.append(f"<tr><td><b>{c['ticker']}</b></td>"
                     f"<td>{c['entry_date'][5:]} @{c['entry']:.2f}</td>"
                     f"<td>{c['exit_date'][5:]} @{c['exit']:.2f}</td>"
                     f"<td>{c['shares']}</td>"
                     f"<td style='color:{col}'>${c['pnl_usd']:+,.0f}</td>"
                     f"<td>{gross_r if gross_r is not None else '—'}R</td>"
                     f"<td class='sub'>${friction:,.0f}</td>"
                     f"<td>{c['r_multiple']}R</td><td>{c['days']}</td>"
                     f"<td class='sub'>{_esc(c['reason'])}</td></tr>"
                     if friction is not None else
                     f"<tr><td><b>{c['ticker']}</b></td>"
                     f"<td>{c['entry_date'][5:]} @{c['entry']:.2f}</td>"
                     f"<td>{c['exit_date'][5:]} @{c['exit']:.2f}</td>"
                     f"<td>{c['shares']}</td>"
                     f"<td style='color:{col}'>${c['pnl_usd']:+,.0f}</td>"
                     f"<td>—</td><td class='sub'>—</td>"
                     f"<td>{c['r_multiple']}R</td><td>{c['days']}</td>"
                     f"<td class='sub'>{_esc(c['reason'])}</td></tr>")
        P.append("</table>")
    else:
        note = ("已有持仓，尚无已结算交易。" if st["positions"] else
                "尚无已结算交易；待成交信号仍在等待有效行情与成交条件。" if st["pending"] else
                "尚无已结算交易；当前无持仓，也无待成交信号。")
        P.append(f"<div class='sub' style='margin-top:4px'>{note}</div>")
    P.append("</div>")
    # —— 开源预告 ——
    P.append(f"<div class='card' style='margin-top:12px;text-align:center'>"
             f"<b>老虎全球资产管理（Tiger Global Asset Management）</b>"
             f"<div class='sub' style='margin-top:4px'>系统主页："
             f"<a href='{_GITHUB_URL}' style='color:{BLUE}'>{_GITHUB_URL}</a><br>"
             f"适时将会开源，敬请关注 ｜ 商业化合作敬请期待</div></div>")
    return "".join(P)


# ---------------------------------------------------------------- 个股深度报告
def _stock_deep_annex(r: "PipelineResult", pick) -> str:
    """深度附录：管道已有数据的二次展示（技术档案/板块内部/赛道联动）。"""
    cand, sec, chain, tech_hit = _pick_context(r, pick)
    currency = _esc(_market_currency(r.raw))
    P: list[str] = []
    P.append("<div class='card' style='margin-top:12px'><b>📊 深度数据档案</b>"
             "<div class='sub' style='margin:2px 0 10px'>以下读数全部来自本轮全链路分析的中间产出，"
             "与决策共用同一数据底座。</div>")

    # —— 个股技术档案 ——
    if cand:
        P.append("<h4 style='margin:6px 0;color:#5b8c00;font-size:13px'>个股技术档案</h4>")
        adv = f"{currency} {cand.adv_usd / 1e6:,.0f}M" if cand.adv_usd else "—"
        P.append("<div class='grid g4'>"
                 f"<div><div class='sub'>日K收盘价</div><div class='kpi' style='font-size:20px'>"
                 f"{currency} {cand.price:.2f}</div></div>"
                 f"<div><div class='sub'>日均成交额</div><div class='kpi' style='font-size:20px'>"
                 f"{adv}</div><div class='sub'>流动性评分 {cand.c_liq}/10</div></div>"
                 f"<div><div class='sub'>日均波动幅度</div><div class='kpi' style='font-size:20px'>"
                 f"{cand.atr_pct:.1%}</div><div class='sub'>止损距离的参考尺</div></div>"
                 f"<div><div class='sub'>综合质量</div><div class='kpi' style='font-size:20px'>"
                 f"{cand.tss_final}/10</div><div class='sub'>扫描初排分 {cand.rank_score:.1f}</div></div>"
                 "</div>")
        P.append("<div style='margin-top:8px'>")
        P.append(_bar_row("价格结构", cand.s_structure, 10.0, GOLD,
                          extra="/10｜趋势形态与关键位质量"))
        P.append(_bar_row("动能", cand.s_momentum, 10.0, BLUE,
                          extra="/10｜涨跌节奏与力度"))
        if cand.s_options is not None:
            P.append(_bar_row("衍生品", cand.s_options, 10.0, "#84cc1688",
                              extra="/10｜期权市场隐含预期"))
        else:
            P.append("<div class='sub'>· 衍生品：本轮数据不可用，已剔除并再归一化</div>")
        P.append("</div>")
        P.append(f"<div class='sub' style='margin-top:6px'>· 关键位（计划入场参考）："
                 f"<b style='color:{FG}'>{cand.key_level:.2f}</b> ｜ 入场形态："
                 f"{_TEMPLATE_ZH.get(cand.entry_template, cand.entry_template or '待定')}</div>")
        if cand.stop_plan:
            P.append(f"<div class='sub'>· 止损计划：{_esc(cand.stop_plan)}</div>")
        if cand.chain_link:
            P.append(f"<div class='sub'>· 产业链环节定位："
                     f"{_LINK_ZH.get(cand.chain_link, cand.chain_link)}</div>")
        for e in (cand.evidence or [])[:4]:
            P.append(f"<div class='sub'>· {_esc(_scrub(_evidence_zh(e)))}</div>")

    # —— 板块内部结构 ——
    if sec:
        f = sec.factors or {}
        driver = max(((k, v) for k, v in f.items() if isinstance(v, (int, float))),
                     key=lambda x: x[1], default=None)
        P.append("<h4 style='margin:14px 0 6px;color:#5b8c00;font-size:13px'>"
                 f"所属板块内部结构（{_etf_zh(sec.etf)}）</h4>")
        P.append("<table><tr><th>读数</th><th>数值</th><th>业务含义</th></tr>"
                 f"<tr><td>板块热度</td><td><b>{sec.shs}/10</b></td>"
                 f"<td class='sub'>{'当前最强主线之一' if sec.in_main_pool else ('次主线（热区链支撑）' if sec.in_sub_pool else '普通板块')}</td></tr>"
                 f"<tr><td>近 20 日相对大盘</td><td><b>{sec.r20:+.1f}%</b></td>"
                 f"<td class='sub'>正数为跑赢本市场基准</td></tr>"
                 f"<tr><td>成分股趋势健康度</td><td><b>{sec.breadth:.0f}%</b></td>"
                 f"<td class='sub'>板块内处于中期上升趋势的个股占比（健康线 60%）</td></tr>"
                 + (f"<tr><td>主要驱动</td><td><b>{_FACTOR_ZH.get(driver[0], driver[0])}</b></td>"
                    f"<td class='sub'>板块动能的第一来源</td></tr>" if driver else "")
                 + "</table>")

    # —— 科技赛道联动 ——
    if tech_hit:
        pros = tech_hit.get("prosperity")
        risk_txt, risk_color = _risk_label(tech_hit.get("risk_level"))
        P.append("<h4 style='margin:14px 0 6px;color:#5b8c00;font-size:13px'>"
                 f"科技赛道联动（{_chain_zh(tech_hit['chain_id'])}）</h4>")
        P.append(f"<div class='sub'>赛道景气 {_prosperity_label(pros)} ｜ 赛道风险 "
                 f"<b style='color:{risk_color}'>{risk_txt}</b>"
                 f"{' ｜ 当前领涨环节：' + _LINK_ZH[tech_hit['leading_link']] if tech_hit.get('leading_link') in _LINK_ZH else ''}"
                 "——赛道景气会为该方向个股评分提供额外支撑。</div>")
        P.append(_transmission_svg(tech_hit))
    P.append("</div>")
    return "".join(P)


def _stock_tab(r: "PipelineResult") -> str:
    """页签·个股深度报告：今日放行标的的完整档案（多标的二级切换）。"""
    research_status = report_research_status(r.raw or {}, r.action)
    if research_status is not None:
        return _research_security_cards(research_status)
    P: list[str] = []
    currency = _esc(_market_currency(r.raw))
    P.append(f"<div class='card' style='border-color:{BLUE}66;margin-bottom:12px'>"
             "<div class='eyebrow-l'>THE STORY BEHIND EACH TRADE · 每笔买卖的来龙去脉</div>"
             f"<b>{_esc(_market_name(r.raw))}个股深度报告</b><div class='sub' style='margin-top:4px'>"
             "今日放行标的的完整分析档案：四层共振结论、五段业务依据、技术档案、"
             "板块内部结构与赛道联动——全部数据与决策同源，逐项可回溯。</div></div>")
    if not r.picks:
        accounting = (_esc(_accounting_note(r.raw)) if _execution_blocked(r.raw) else
                      "本轮未产生新的放行信号；既有持仓继续按出场规则管理。")
        P.append("<div class='card'><b>今日无放行标的</b><div class='sub' style='margin-top:6px'>"
                 + accounting +
                 "以下为本轮观察名单前列标的的质量档案，后续仍须重新通过各项闸门。</div></div>")
        for c in r.watchlist[:3]:
            nm = _name_zh(c.ticker)
            nm_html = (f"<span style='font-size:14px;color:{GOLD};margin-left:4px'>"
                       f"{_esc(nm)}</span>") if nm else ""
            P.append(f"<div class='card' style='margin-top:10px'>"
                     f"<div style='display:flex;justify-content:space-between;align-items:baseline'>"
                     f"<div><b style='font-size:16px'>{c.ticker}</b>{nm_html}</div>"
                     f"<span class='tag'>综合质量 {c.tss_final}/10</span></div>"
                     f"<div class='sub' style='margin-top:4px'>所属板块 {_etf_zh(c.sector_etf)}"
                     f" ｜ 所属赛道 {_chain_zh(c.chain_id)}"
                     f" ｜ 日K收盘价 <b style='color:{GOLD}'>{currency} {c.price:.2f}</b>"
                     f" ｜ 入场形态 {_TEMPLATE_ZH.get(c.entry_template, c.entry_template or '待定')}"
                     f" ｜ 关键位 {c.key_level:.2f}</div>")
            P.append(_bar_row("价格结构", c.s_structure, 10.0, GOLD, extra="/10"))
            P.append(_bar_row("动能", c.s_momentum, 10.0, BLUE, extra="/10"))
            for e in (c.evidence or [])[:2]:
                P.append(f"<div class='sub'>· {_esc(_scrub(_evidence_zh(e)))}</div>")
            P.append("</div>")
        if not r.watchlist:
            P.append("<div class='card sub' style='margin-top:10px'>预备观察名单为空——"
                     "本轮扫描池未筛出达标候选，等待新信号。</div>")
        return "".join(P)

    # —— 多标的二级切换 ——
    if len(r.picks) > 1:
        P.append("<div style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px'>")
        for i, p in enumerate(r.picks):
            P.append(f"<button class='subbtn{' active' if i == 0 else ''}' id='stock-btn-{i}' "
                     f"onclick='showStock({i})'>#{i + 1} {p.ticker}</button>")
        P.append("</div>")
    for i, pick in enumerate(r.picks):
        P.append(f"<div id='stock-pane-{i}' style='display:{'' if i == 0 else 'none'}'>")
        # —— 公司横幅：黑金徽章（logo 位）+ 所属板块/赛道 + 大号日K收盘价 + 产业链主题产品图 ——
        cand_b, _, _, _ = _pick_context(r, pick)
        cur_b = cand_b.price if (cand_b and cand_b.price) else None
        price_block = (f"<div style='text-align:center;padding:0 10px;flex:none'>"
                       f"<div class='sub'>日K收盘价</div>"
                       f"<div style='font-size:32px;font-weight:900;color:{GOLD};"
                       f"line-height:1.1'>{f'{currency} {cur_b:.2f}' if cur_b else '—'}</div>"
                       f"<div class='sub'>本轮最新读数</div></div>")
        P.append(f"<div class='card' style='margin-bottom:12px;display:flex;gap:18px;"
                 f"align-items:center;flex-wrap:wrap;border-color:#d4af3788'>"
                 f"{_ticker_badge_svg(pick.ticker, market=_market_name(r.raw))}"
                 f"<div style='flex:1;min-width:min(220px,100%)'>"
                 f"<div style='font-size:22px;font-weight:800'>{_esc(pick.ticker)}"
                 + (f"<span style='font-size:17px;color:{GOLD};margin-left:6px'>"
                    f"{_esc(_name_zh(pick.ticker))}</span>" if _name_zh(pick.ticker) else "")
                 + f"<span class='tag' style='margin-left:8px'>今日放行 #{i + 1}</span></div>"
                 f"<div class='sub' style='margin-top:4px'>所属板块 "
                 f"{_esc(_etf_zh(pick.sector))} ｜ 所属赛道 "
                 f"{_esc(_chain_zh(pick.chain))}</div></div>"
                 f"{price_block}"
                 f"{_theme_art_html(pick.chain)}"
                 f"</div>")
        P.append(_decision_card(r, pick))
        P.append(_stock_deep_annex(r, pick))
        P.append("</div>")
    return "".join(P)


# ---------------------------------------------------------------- 策略验证中心
def _closed_rs(entries: list[dict]) -> list[float]:
    """已结算信号的 R 序列（时间正序）。"""
    return [e["r"] for e in entries
            if e.get("status") == "closed" and e.get("r") is not None]


def _hold_days(e: dict) -> int | None:
    """持有天数（自然日，近似）。"""
    try:
        from datetime import date as _d
        a = [int(x) for x in e["entry_date"].split("-")]
        b = [int(x) for x in e["exit_date"].split("-")]
        return (_d(*b) - _d(*a)).days
    except Exception:
        return None


def _agg_rs(rs: list[float]) -> dict:
    """一组 R 的四项读数。"""
    if not rs:
        return {"n": 0, "win_rate": 0.0, "avg": 0.0, "total": 0.0}
    return {"n": len(rs),
            "win_rate": sum(1 for x in rs if x > 0) / len(rs),
            "avg": sum(rs) / len(rs), "total": sum(rs)}


def _empty_panel_svg(text: str, sub: str = "", width: int = 1080,
                     height: int = 200) -> str:
    """黑金面板的优雅空态（数据未积累时不留白、不编造）。"""
    return (f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto' "
            f"aria-hidden='true'>"
            f"<rect width='{width}' height='{height}' rx='16' fill='#0e0e11' "
            f"stroke='#d4af3744' stroke-width='1.5'/>"
            f"<circle cx='{width / 2}' cy='{height / 2 - 22}' r='16' fill='none' "
            f"stroke='#d4af37' stroke-width='1.6' stroke-dasharray='4 5'/>"
            f"<circle cx='{width / 2}' cy='{height / 2 - 22}' r='5' fill='#ffd700'/>"
            f"<text x='{width / 2}' y='{height / 2 + 22}' text-anchor='middle' "
            f"font-size='16' fill='#cbb26a' font-weight='600'>{text}</text>"
            + (f"<text x='{width / 2}' y='{height / 2 + 48}' text-anchor='middle' "
               f"font-size='12' fill='#8a8a7a'>{sub}</text>" if sub else "")
            + "</svg>")


def _verify_curve_svg(sim: dict | None, bench: dict | None,
                      width: int = 1080, height: int = 260) -> str:
    """在各序列都已开始的共同窗口比较；只前向延续真实观测。"""
    state = (sim or {}).get("state") or {}
    curve = state.get("equity_curve") or []
    series: dict[str, dict[str, float]] = {}
    # 初始资金字段并不是带日期的净值记录，不拿它补曲线上的空值。
    strategy = {p["date"]: _number(p.get("equity")) for p in curve if p.get("date")}
    if any(v is None for v in strategy.values()):
        return _empty_panel_svg("净值 vs 基准对比曲线", "净值记录存在缺失或无效读数，无法绘制")
    if len(strategy) < 2:
        return _empty_panel_svg(
            "净值 vs 基准对比曲线", "至少两个不同日期的净值记录后绘制，无需等待交易结算")
    series["小虎模拟盘"] = strategy
    for name in ("QQQ", "SPY"):
        pts = (bench or {}).get(name) or []
        if pts:
            values = {d: _number(c) for d, c in pts if d}
            if any(v is None for v in values.values()):
                return _empty_panel_svg("净值 vs 基准对比曲线", f"{name} 存在缺失或无效读数，无法比较")
            if values:
                series[name] = values
    start = max(min(s) for s in series.values())
    end = min(max(s) for s in series.values())
    if start > end:
        return _empty_panel_svg("净值 vs 基准对比曲线", "无共同日期窗口，暂不比较")
    all_dates = sorted({d for s in series.values() for d in s if start <= d <= end})
    if len(all_dates) < 2:
        return _empty_panel_svg("净值 vs 基准对比曲线", "共同窗口不足两个日期，暂不比较")
    filled: dict[str, dict[str, float]] = {}
    last_observed: dict[str, str] = {}
    for name, s in series.items():
        dated = sorted((d, v) for d, v in s.items() if d <= end)
        cursor = 0
        last = None
        ff = {}
        for d in all_dates:
            while cursor < len(dated) and dated[cursor][0] <= d:
                last_observed[name], last = dated[cursor]
                cursor += 1
            if last is not None:
                ff[d] = last
        if ff:
            b0 = ff[all_dates[0]]
            if b0 <= 0:
                return _empty_panel_svg("净值 vs 基准对比曲线", f"{name} 窗口起点读数非正，无法归一化")
            filled[name] = {d: v / b0 - 1.0 for d, v in ff.items()}
    vals = [v for s in filled.values() for v in s.values()]
    if not vals or len(all_dates) < 2:
        return _empty_panel_svg("净值 vs 基准对比曲线", "数据积累中")
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    pad = (hi - lo) * 0.12 or 0.01
    lo, hi = lo - pad, hi + pad
    x0, x1, y0, y1 = 56, width - 30, 26, height - 40

    def _xy(d, v):
        i = all_dates.index(d)
        x = x0 + (x1 - x0) * i / (len(all_dates) - 1)
        y = y0 + (y1 - y0) * (1 - (v - lo) / (hi - lo))
        return x, y

    colors = {"小虎模拟盘": ("#ffd700", 3.2, ""), "QQQ": ("#7ea6ff", 2, ""),
              "SPY": ("#9ca3af", 2, "stroke-dasharray='5 4'")}
    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto'>",
             f"<rect width='{width}' height='{height}' rx='16' fill='#0e0e11' "
             f"stroke='#d4af3744' stroke-width='1.5'/>"]
    # 零轴 + 网格
    zero_y = y0 + (y1 - y0) * (1 - (0 - lo) / (hi - lo))
    parts.append(f"<line x1='{x0}' y1='{zero_y:.1f}' x2='{x1}' y2='{zero_y:.1f}' "
                 f"stroke='#cbb26a' stroke-opacity='0.5' stroke-dasharray='3 5'/>")
    for gy in (0.25, 0.5, 0.75):
        yy = y0 + (y1 - y0) * gy
        parts.append(f"<line x1='{x0}' y1='{yy:.0f}' x2='{x1}' y2='{yy:.0f}' "
                     f"stroke='#ffffff' stroke-opacity='0.06'/>")
    # 三线 + 末端读数（标签先收集后防碰撞排布）
    lx = x0 + 14
    labels = []
    for name, (col, w, dash) in colors.items():
        s = filled.get(name)
        if not s:
            continue
        pts = " ".join(f"{_xy(d, v)[0]:.1f},{_xy(d, v)[1]:.1f}" for d, v in s.items())
        parts.append(f"<polyline points='{pts}' fill='none' stroke='{col}' "
                     f"stroke-width='{w}' {dash} stroke-linejoin='round'/>")
        last_d = list(s)[-1]
        ex, ey = _xy(last_d, s[last_d])
        parts.append(f"<circle cx='{ex:.1f}' cy='{ey:.1f}' r='4' fill='{col}'/>")
        labels.append([ey + 4, col, f"{s[last_d]:+.1%}", ex])
        parts.append(f"<rect x='{lx}' y='8' width='10' height='10' rx='3' fill='{col}'/>")
        parts.append(f"<text x='{lx + 16}' y='17' font-size='12' fill='#cbb26a'>{name}</text>")
        lx += 116
    labels.sort()
    for i in range(1, len(labels)):  # 纵向最小间距 15px，防止末端标签互相叠字
        if labels[i][0] - labels[i - 1][0] < 15:
            labels[i][0] = labels[i - 1][0] + 15
    for ly, col, txt, ex in labels:
        if ex > width - 110:  # 末端贴右缘时右对齐回收，防溢出画布
            parts.append(f"<text x='{width - 12}' y='{ly:.1f}' text-anchor='end' "
                         f"font-size='12' fill='{col}' font-weight='700'>{txt}</text>")
        else:
            parts.append(f"<text x='{ex + 8:.0f}' y='{ly:.1f}' "
                         f"font-size='12' fill='{col}' font-weight='700'>{txt}</text>")
    parts.append(f"<text x='{width - 16}' y='{height - 12}' text-anchor='end' "
                 f"font-size='11' fill='#8a8a7a'>共同窗口 {start} 至 {end} ｜ 起点归一</text>")
    parts.append("</svg>")
    dates_note = "；".join(f"{name} 最后真实记录 {day}" for name, day in last_observed.items())
    parts.append(f"<div class='sub'>{_esc(dates_note)}。窗口内缺少当日记录时只延续此前观测，"
                 "不补出更早的价格，也不把延续值作为新行情。</div>")
    return "".join(parts)


def _r_hist_svg(rs: list[float], width: int = 1080, height: int = 200) -> str:
    """已结算信号的 R 分布直方图（截断亏损、让利润奔跑的结构证据）。"""
    if not rs:
        return _empty_panel_svg("R 分布直方图", "首笔信号结算后自动绘制", width, height)
    lo_b, hi_b, step = -2.0, 4.0, 0.5
    bins = int((hi_b - lo_b) / step)
    counts = [0] * bins
    for x in rs:
        i = min(max(int((x - lo_b) / step), 0), bins - 1)
        counts[i] += 1
    peak = max(counts) or 1
    x0, y1, bw = 40, height - 34, (width - 60) / bins
    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto'>",
             f"<rect width='{width}' height='{height}' rx='16' fill='#0e0e11' "
             f"stroke='#d4af3744' stroke-width='1.5'/>"]
    for i, n in enumerate(counts):
        if not n:
            continue
        mid = lo_b + (i + 0.5) * step
        h = (y1 - 20) * n / peak
        col = "#16a34a" if mid > 0 else "#dc2626"
        parts.append(f"<rect x='{x0 + i * bw + 3:.1f}' y='{y1 - h:.1f}' "
                     f"width='{bw - 6:.1f}' height='{h:.1f}' rx='4' fill='{col}' "
                     f"fill-opacity='0.85'/>")
        parts.append(f"<text x='{x0 + i * bw + bw / 2:.1f}' y='{y1 - h - 4:.1f}' "
                     f"text-anchor='middle' font-size='11' fill='#cbb26a'>{n}</text>")
        parts.append(f"<text x='{x0 + i * bw + bw / 2:.1f}' y='{y1 + 16:.1f}' "
                     f"text-anchor='middle' font-size='10' fill='#8a8a7a'>"
                     f"{mid:+.1f}</text>")
    parts.append(f"<text x='{width - 14}' y='20' text-anchor='end' font-size='11' "
                 f"fill='#8a8a7a'>横轴：R（风险倍数）｜ 纵轴：笔数</text>")
    parts.append("</svg>")
    return "".join(parts)


def _month_heat_html(closed: list[dict]) -> str:
    """月度盈亏热力（按离场月归集）。"""
    by_m: dict[str, list[float]] = {}
    for e in closed:
        m = (e.get("exit_date") or "")[:7]
        if m:
            by_m.setdefault(m, []).append(e["r"])
    if not by_m:
        return ("<div class='card sub' style='text-align:center;padding:18px'>"
                "首笔信号结算后，这里按月展示盈亏热力。</div>")
    peak = max(abs(sum(v)) for v in by_m.values()) or 1.0
    chips = []
    for m in sorted(by_m):
        tot = sum(by_m[m])
        inten = min(abs(tot) / peak, 1.0)
        col = "22,163,74" if tot > 0 else "220,38,38"
        chips.append(
            f"<div style='flex:1;min-width:120px;background:rgba({col},"
            f"{0.08 + 0.3 * inten:.2f});border:1px solid rgba({col},0.45);"
            f"border-radius:12px;padding:10px;text-align:center'>"
            f"<div class='sub'>{m}</div>"
            f"<div style='font-size:19px;font-weight:800;color:rgb({col})'>"
            f"{tot:+.2f}R</div><div class='sub'>{len(by_m[m])} 笔</div></div>")
    return ("<div style='display:flex;gap:10px;flex-wrap:wrap'>"
            + "".join(chips) + "</div>")


def _slice_table(title: str, groups: list[tuple[str, list[float]]]) -> str:
    """归因切片表：维度 → 笔数/胜率/期望R/累计R（期望着色斑马条）。"""
    rows = [(label, _agg_rs(rs)) for label, rs in groups if rs]
    if not rows:
        return ""
    peak = max(abs(a["avg"]) for _, a in rows) or 1.0
    trs = []
    for label, a in sorted(rows, key=lambda x: x[1]["total"]):
        col = GREEN if a["avg"] > 0 else RED
        w = 8 + 60 * abs(a["avg"]) / peak
        trs.append(
            f"<tr><td>{label}</td><td>{a['n']}</td>"
            f"<td>{a['win_rate']:.0%}</td>"
            f"<td style='color:{col};font-weight:700'>{a['avg']:+.2f}R</td>"
            f"<td style='color:{col}'>{a['total']:+.2f}R</td>"
            f"<td style='min-width:80px'><div style='height:8px;width:{w:.0f}px;"
            f"border-radius:4px;background:{col};opacity:0.75'></div></td></tr>")
    return (f"<div class='card' style='margin-top:10px'><b>{title}</b><table "
            f"style='margin-top:6px'><tr><th>维度</th><th>笔数</th><th>胜率</th>"
            f"<th>期望</th><th>累计</th><th></th></tr>{''.join(trs)}</table></div>")


def _failure_modes_html(closed: list[dict]) -> str:
    """失败模式 TopN：亏损单的共性聚类（赛道×形态，n≥3 且期望为负）。"""
    combos: dict[tuple[str, str], list[float]] = {}
    for e in closed:
        chain = _chain_zh(e["chain"]) if e.get("chain") else "未记录赛道"
        tmpl = _TEMPLATE_ZH.get(e.get("template") or "", e.get("template") or "无形态")
        combos.setdefault((chain, tmpl), []).append(e["r"])
    bad = [(k, _agg_rs(v)) for k, v in combos.items()
           if len(v) >= 3 and _agg_rs(v)["avg"] < 0]
    bad.sort(key=lambda x: x[1]["total"])
    if not bad:
        return ("<div class='card' style='margin-top:10px;border-color:#16a34a55'>"
                "<b style='color:#16a34a'>✓ 未发现显著失败模式</b>"
                "<div class='sub' style='margin-top:4px'>亏损单未呈现可聚类的共性"
                "（赛道×形态 组合样本 ≥3 且期望为负才会亮红牌）。随样本积累，"
                "此面板自动盯防策略的结构性失血点。</div></div>")
    cards = []
    for (chain, tmpl), a in bad[:3]:
        cards.append(
            f"<div class='card' style='flex:1;min-width:min(220px,100%);border-color:{RED}55'>"
            f"<b style='color:{RED}'>✕ {chain} × {tmpl}</b>"
            f"<div class='sub' style='margin-top:4px'>{a['n']} 笔 ｜ 胜率 "
            f"{a['win_rate']:.0%} ｜ 期望 <b style='color:{RED}'>{a['avg']:+.2f}R</b>"
            f" ｜ 累计 {a['total']:+.2f}R</div>"
            f"<div class='sub' style='margin-top:4px'>→ 建议纳入下期复盘假设单："
            f"该组合是否应降级或回避？经滚动前推验证后方可动刀。</div></div>")
    return ("<div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:10px'>"
            + "".join(cards) + "</div>")


def _lifecycle_card(e: dict) -> str:
    """信号全生命周期档案卡：状态机时间轴 + 当时依据 vs 事后结果。"""
    ticker = e.get("ticker", "")
    nm = _name_zh(ticker)
    closed = e.get("status") == "closed" and e.get("r") is not None
    void = e.get("status") == "void"
    has_entry = e.get("entry") is not None
    # —— 状态机四节点 ——
    steps = [("已放行", e.get("date", "")),
             ("成交", f"{e.get('entry_date', '')} @{e.get('entry')}" if has_entry else ""),
             ("离场", f"{e.get('exit_date', '')} @{e.get('exit')}" if closed else ""),
             ("结算", f"{e['r']:+.2f}R" if closed else "")]
    done_upto = 3 if closed else (1 if has_entry else 0)
    nodes = []
    for i, (label, sub) in enumerate(steps):
        done = i <= done_upto
        dot = (f"<span style='display:inline-block;width:14px;height:14px;border-radius:50%;"
               f"background:{'#ffd700' if done else 'transparent'};border:2px solid "
               f"{'#d4af37' if done else '#c9c9b8'};vertical-align:middle'></span>")
        line = (f"<span style='display:inline-block;flex:1;height:2px;min-width:12px;"
                f"background:{'#d4af37' if i < done_upto else '#d9d9c8'};margin:0 4px'></span>"
                if i < 3 else "")
        sub_txt = sub if sub else (
            "已作废" if void and i == 1 else "—" if void else
            "待成交" if i == 1 else "持仓中" if i == 2 and has_entry else "—")
        nodes.append(f"<div style='display:flex;align-items:center'>"
                     f"<div style='text-align:center;min-width:52px'>{dot}"
                     f"<div style='font-size:12px;font-weight:700;margin-top:2px'>{label}</div>"
                     f"<div class='sub' style='font-size:11px'>{sub_txt}</div></div>{line}</div>")
    # —— 结果与依据 ——
    if void:
        r_html = "<div class='sub' style='font-size:15px'>已作废</div>"
    elif closed:
        col = GREEN if e.get("win") else RED
        r_html = f"<div style='font-size:26px;font-weight:900;color:{col}'>{e['r']:+.2f}R</div>"
    elif has_entry and e.get("r_live") is not None:
        col = GREEN if e["r_live"] > 0 else RED
        r_html = (f"<div style='font-size:26px;font-weight:900;color:{col}'>"
                  f"{e['r_live']:+.2f}R</div><div class='sub'>浮动（未结算，不计入胜率）</div>")
    elif has_entry:
        r_html = "<div class='sub' style='font-size:15px'>持仓中（浮动结果未提供）</div>"
    else:
        r_html = "<div class='sub' style='font-size:15px'>待成交</div>"
    meta = [f"模式 {e.get('mode', '-')}",
            f"形态 {_TEMPLATE_ZH.get(e.get('template') or '', e.get('template') or '无')}",
            f"板块 {_etf_zh(e['sector'])}" if e.get("sector") else "",
            f"赛道 {_chain_zh(e['chain'])}" if e.get("chain") else "",
            f"质量 {e.get('tss_final')}/10", f"当时环境 {e.get('mrs_star')}/10"]
    meta = [m for m in meta if m]
    tail = []
    if e.get("note"):
        tail.append(f"离场原因：<b>{_esc(e['note'])}</b>")
    hd = _hold_days(e)
    if hd is not None:
        tail.append(f"持有 {hd} 天")
    if e.get("entry_ref"):
        tail.append(f"入场参考 {e['entry_ref']} ｜ 止损 {e.get('stop')}")
    return (f"<div class='card' style='margin-top:10px'>"
            f"<div style='display:flex;justify-content:space-between;align-items:flex-start;"
            f"gap:10px;flex-wrap:wrap'>"
            f"<div><b style='font-size:17px'>{ticker}</b>"
            + (f" <span style='color:{GOLD};font-weight:600'>{_esc(nm)}</span>" if nm else "")
            + f"</div>{r_html}</div>"
            f"<div class='lifecycle-steps'>"
            f"{''.join(nodes)}</div>"
            f"<div class='sub'>{' ｜ '.join(meta)}</div>"
            + (f"<div class='sub' style='margin-top:2px'>{' ｜ '.join(tail)}</div>"
               if tail else "")
            + "</div>")


def _ledger_table(entries: list[dict]) -> str:
    """全量台账（逐笔可复核，折叠收纳）。"""
    rows = []
    for e in reversed(entries[-60:]):
        status, result = "", ""
        if e.get("status") == "closed":
            status = f"已结算<br><span class='sub'>{e.get('exit_date', '')} @{e.get('exit')}</span>"
            r_mult = e.get("r")
            if r_mult is not None:
                col = GREEN if e.get("win") else RED
                result = f"<b style='color:{col}'>{r_mult:+.2f}R</b>"
        elif e.get("status") == "void":
            status = "<span class='sub'>已作废</span>"
        elif e.get("status") == "open" and e.get("entry") is not None:
            status = (f"持仓中<br><span class='sub'>{e.get('entry_date', '')} "
                      f"@{e.get('entry')}</span>")
        elif e.get("status") == "open":
            status = "<span class='sub'>待成交</span>"
        else:
            status = f"<span class='sub'>状态未知（{_esc(e.get('status') or '未提供')}）</span>"
        rows.append(f"<tr><td>{e.get('date', '')}</td><td><b>{e.get('ticker', '')}</b></td>"
                    f"<td>{e.get('mode', '')}</td><td>{e.get('entry_ref')}</td>"
                    f"<td style='color:{RED}'>{e.get('stop')}</td>"
                    f"<td>{e.get('tss_final')}/10</td><td>{e.get('mrs_star')}/10</td>"
                    f"<td>{status}</td><td>{result}</td></tr>")
    return ("<table><tr><th>信号日期</th><th>标的</th><th>模式</th><th>入场参考</th>"
            "<th>止损</th><th>质量</th><th>当时环境</th><th>状态</th><th>结果</th></tr>"
            + "".join(rows) + "</table>")


def _verify_tab(entries: list[dict] | None, stats: dict | None,
                bench: dict | None, sim: dict | None) -> str:
    """页签·策略验证中心：证明（成绩单）→ 追溯（生命周期）→ 诊断（归因实验室）。"""
    P: list[str] = []
    entries = entries or []
    stats = stats or {}
    closed = [e for e in entries if e.get("status") == "closed" and e.get("r") is not None]
    rs = [e["r"] for e in closed]
    n_closed = len(closed)
    open_entries = [e for e in entries if e.get("status") == "open"]
    held_n = sum(e.get("entry") is not None for e in open_entries)
    pending_n = len(open_entries) - held_n

    # —— 门头：三章承诺（数据诚实公开亮牌）——
    P.append(f"<div class='card' style='border-color:{BLUE}66;margin-bottom:12px'>"
             "<div class='eyebrow-l'>EVIDENCE, NOT OPINIONS. · 拿证据，不甩观点</div>"
             "<b>📊 策略验证中心</b><div class='sub' style='margin-top:4px'>"
             "回溯不是记账，是持续回答三个问题：<b>① 策略有效吗？② 对在哪、错在哪？"
             "③ 修正之后变好了吗？</b>这里的一切读数来自真实落账的台账与模拟盘，"
             "逐笔可复核。</div>"
             "<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:8px'>"
             f"<span class='tag'>次日开盘价成交 · 无未来函数</span>"
             f"<span class='tag'>逐笔留痕 · 亏赚如实</span>"
             f"<span class='tag' style='color:#a8842c;border-color:#d4af3766'>"
             f"未满 100 笔不作有效性结论（{n_closed}/100）</span></div></div>")

    # ==================== A. 策略成绩单（证明层）====================
    P.append("<h2>A · 策略成绩单</h2>")
    # DSR 亮牌
    if n_closed < 100:
        dsr = (f"<span class='tag' style='color:#a8842c;border-color:#d4af3766'>"
               f"统计显著性：样本积累中 · 首个 DSR 检查点 {_DSR_CHECKPOINT}</span>")
    else:
        mean = sum(rs) / n_closed
        var = sum((x - mean) ** 2 for x in rs) / (n_closed - 1)
        t = mean / math.sqrt(var / n_closed) if var > 0 else 0.0
        ok = t >= 2.0
        dsr = (f"<span class='tag' style='color:{GREEN if ok else RED};"
               f"border-color:{GREEN if ok else RED}66'>统计显著性观察：t={t:.2f}"
               f"（{'显著' if ok else '未达显著'}，{_DSR_CHECKPOINT} DSR 复核为准）</span>")
    # 最大回撤（累计 R 曲线峰谷）
    cum = peak = 0.0
    mdd = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    pf = stats.get("profit_factor", 0.0)
    pf_txt = "∞" if pf == float("inf") else f"{pf}"
    note = ("<div class='sub' style='margin:-6px 0 10px'>过程快照 · 样本积累中，"
            "不作有效性结论（公开验证章程纪律）。</div>" if n_closed < 100 else "")
    P.append(f"<div class='grid g3' style='margin-bottom:4px'>"
             f"<div class='card'><div class='sub'>已结算信号</div>"
             f"<div class='kpi'>{n_closed}</div>"
             f"<div class='sub'>持仓中 {held_n} 笔，待成交 {pending_n} 笔</div></div>"
             f"<div class='card'><div class='sub'>总胜率</div>"
             f"<div class='kpi'>{stats.get('win_rate', 0):.1%}</div>"
             f"<div class='sub'>近20笔 {stats.get('last20', {}).get('win_rate', 0):.0%}</div></div>"
             f"<div class='card'><div class='sub'>平均期望</div>"
             f"<div class='kpi'>{stats.get('expectancy_r', 0)}R</div>"
             f"<div class='sub'>累计 {stats.get('total_r', 0)}R</div></div>"
             f"<div class='card'><div class='sub'>盈亏比</div>"
             f"<div class='kpi'>{pf_txt}</div><div class='sub'>总盈利 ÷ 总亏损</div></div>"
             f"<div class='card'><div class='sub'>最大回撤</div>"
             f"<div class='kpi' style='color:{RED}'>{mdd:.2f}R</div>"
             f"<div class='sub'>累计R曲线峰谷</div></div>"
             f"<div class='card'><div class='sub'>统计显著性</div>"
             f"<div style='margin-top:8px'>{dsr}</div></div></div>{note}")
    P.append(f"<div class='card' style='padding:14px 10px 6px'>"
             f"{_verify_curve_svg(sim, bench)}</div>")
    P.append("<div style='display:flex;gap:14px;flex-wrap:wrap'>"
             f"<div class='card' style='flex:2;min-width:min(340px,100%);padding:14px 10px 6px'>"
             f"{_r_hist_svg(rs)}</div>"
             f"<div class='card' style='flex:1;min-width:min(300px,100%)'><b>月度盈亏热力</b>"
             f"<div style='margin-top:8px'>{_month_heat_html(closed)}</div></div></div>")

    # ==================== B. 信号全生命周期（追溯层）====================
    P.append("<h2>B · 信号全生命周期</h2>")
    if entries:
        P.append("<div class='sub' style='margin-bottom:6px'>每笔信号一张档案卡："
                 "状态机四节点（已放行 → 成交 → 离场 → 结算），当时依据与事后结果并排，"
                 "最近 30 笔。</div>")
        for e in reversed(entries[-30:]):
            P.append(_lifecycle_card(e))
    else:
        P.append("<div class='card sub'>账本从今天开始积累——首个推荐信号落账后，"
                 "这里将逐笔展示全生命周期档案。</div>")

    # ==================== C. 盈亏归因实验室（诊断层）====================
    P.append("<h2>C · 盈亏归因实验室</h2>")
    P.append("<div class='sub' style='margin-bottom:6px'>把已结算信号按不同维度切片，"
             "找到策略的能力圈与百慕大；红牌为自动聚类出的失败模式。</div>")
    P.append(_failure_modes_html(closed))

    def _mrs_band(v) -> str:
        if v is None:
            return "未记录"
        return "强环境（≥7.5）" if v >= 7.5 else ("中环境（6–7.5）" if v >= 6.0 else "弱环境（<6）")

    def _hold_band(e) -> str:
        d = _hold_days(e)
        if d is None:
            return "未知"
        return "≤3 天" if d <= 3 else ("4–7 天" if d <= 7 else "8 天以上")

    # 逐维度分组
    def _group(keyfn):
        g: dict[str, list[float]] = {}
        for e in closed:
            g.setdefault(keyfn(e), []).append(e["r"])
        return sorted(g.items())

    P.append(_slice_table("切片 · 入场形态", _group(
        lambda e: _TEMPLATE_ZH.get(e.get("template") or "", e.get("template") or "无"))))
    P.append(_slice_table("切片 · 产业链赛道", _group(
        lambda e: _chain_zh(e["chain"]) if e.get("chain") else "未记录（旧台账）")))
    P.append(_slice_table("切片 · 板块", _group(
        lambda e: _etf_zh(e["sector"]) if e.get("sector") else "未记录（旧台账）")))
    P.append(_slice_table("切片 · 市场环境档位", _group(lambda e: _mrs_band(e.get("mrs_star")))))
    P.append(_slice_table("切片 · 离场原因", _group(lambda e: e.get("note") or "其他")))
    P.append(_slice_table("切片 · 持有天数", _group(_hold_band)))

    # ==================== D. 全量台账（折叠）====================
    if entries:
        P.append("<details style='margin-top:14px'><summary><b>D · 全量台账</b>"
                 "（逐笔原始记录，最近 60 笔）</summary>"
                 f"<div class='card' style='margin-top:8px'>{_ledger_table(entries)}"
                 "<div class='sub' style='margin-top:8px'>读法：「当时环境」为信号日的"
                 "市场环境综合评级（满分 10）；结果以 R（风险倍数）计——+2R 表示盈利两倍于"
                 "初始风险，-1R 表示亏损等于初始风险。更早记录完整保存在系统账本中。</div>"
                 "</div></details>")
    return "".join(P)


# ---------------------------------------------------------------- 交易理念
def _philosophy_tab(raw: dict | None = None, *, asset_dir: str | os.PathLike | None = None) -> str:
    """页签·核心交易理念：按《AI短线美股交易（1-15天波段版）白皮书》整理。"""
    raw = raw or {}
    market = _esc(_market_name(raw))
    P: list[str] = []
    P.append(f"<div class='card' style='border-color:{GOLD}66;margin-bottom:12px;text-align:center'>"
             "<div class='eyebrow-l'>DISCIPLINE IS THE EDGE. · 纪律就是优势</div>"
             f"<div style='font-size:17px'><b>汇聚顶级基金经理思想 × AI 能力的{market}交易系统</b></div>"
             f"<div class='sub' style='margin-top:6px'>投资标的：<b style='color:{FG}'>{market}</b>"
             f"（{_esc(_universe_note(raw))}）<br>"
             "四层共振 × 数据工程 × 自我迭代的全链路交易系统 ｜ 波段周期 1–15 天<br>"
             f"<b style='color:{GOLD}'>No prediction. Process, discipline, audit.</b>"
             "——Edge 不是预测，是流程。</div></div>")

    def _idea(no: str, title: str, body: str) -> str:
        return (f"<div class='card' style='margin-top:10px'><b>{no} {title}</b>"
                f"<div class='sub' style='margin-top:6px;line-height:1.9'>{body}</div></div>")

    # —— 每天只回答三个问题 ——
    P.append("<h2>每天只回答三个问题</h2>")
    P.append("<div class='grid g3'>"
             f"<div class='card'><b style='color:{GOLD}'>① 今天适不适合冒险？</b>"
             "<div class='sub' style='margin-top:6px'>若市场处于「惩罚持仓」状态——利率施压、广度"
             "塌陷、情绪脆弱——再强的选股也会被一波带走。所以第一件事不是选股，而是定今天的"
             "<b>风险预算</b>：市场环境体检（五维投票）直接映射为总仓位上限与开仓许可，"
             "不达标宁可不做。</div></div>"
             f"<div class='card'><b style='color:{GOLD}'>② 钱在炒哪个方向？</b>"
             "<div class='sub' style='margin-top:6px'>散户最经典的死法：「在弱板块里挑到最强的"
             "那只」。板块是发动机，个股只是车——系统强制聚焦 1–2 条资金主线，并确认所属"
             "产业链处于景气热区，只在资金推进的方向里做事。</div></div>"
             f"<div class='card'><b style='color:{GOLD}'>③ 错了怎么走、对了怎么赚？</b>"
             "<div class='sub' style='margin-top:6px'>真正的交易水平不是「多相信它会涨」，而是"
             "下单前写清两句话：<b>我错了在哪里认错</b>（止损锚点）、<b>我对了怎么把利润拿走</b>"
             "（推进与保护）。只在结构清晰、亏损可控、盈亏比划算的位置出手。</div></div></div>")
    P.append(f"<div class='card' style='margin-top:10px;text-align:center'>"
             "<b>先拿交易许可，再选主线赛道，确认产业链景气，最后才谈买点刀口。</b>"
             "<div class='sub' style='margin-top:4px'>不天天交易——只在胜率与赔率同时出现时出手。"
             "这是一套响应系统，不是预测系统。</div></div>")

    # —— 四层共振（驾车比喻 + 倒金字塔图示）——
    P.append("<h2>四层共振决策（用驾车理解）</h2>")
    P.append(f"<div class='card' style='padding:14px 10px 4px'>{_resonance_svg()}</div>")
    P.append("<div class='card'><table>"
             "<tr><th>层</th><th>驾车比喻</th><th>回答的问题</th></tr>"
             "<tr><td><b>市场环境</b></td><td>天气与限速</td><td>决定你今天开不开车——"
             "定总仓位上限与新开仓许可</td></tr>"
             "<tr><td><b>板块主线</b></td><td>导航里的主干道</td><td>决定你走哪条路——"
             "主线池最多 1–2 条，拒绝分散</td></tr>"
             "<tr><td><b>产业链周期</b></td><td>这条路的服务区分布</td><td>决定你在哪一段效率最高——"
             "利润正从上中下游哪个环节传导</td></tr>"
             "<tr><td><b>个股结构</b></td><td>具体路口的并线与刹车</td><td>决定是否安全且快——"
             "这笔交易值不值做</td></tr></table></div>")

    # —— 六层决策栈（流水线图示 + 表）——
    P.append("<h2>六层决策栈：每层只回答一个问题</h2>")
    P.append(f"<div class='card' style='padding:16px 8px 6px'>{_pipeline_svg()}</div>")
    P.append("<div class='card'><table>"
             "<tr><th>层</th><th>回答的问题</th><th>输出</th></tr>"
             "<tr><td>股票池扫描</td><td>本轮扫描池中，谁值得精评？</td>"
             "<td>候选数量按本轮配置与数据可用性确定，另含主线定向补扫</td></tr>"
             "<tr><td>市场许可</td><td>今天最多能押多少？</td>"
             "<td>总仓位上限 + 新开仓许可（五维投票，意见打架自动打折）</td></tr>"
             "<tr><td>主线识别</td><td>钱在哪 1–2 条主线？</td>"
             "<td>主线池 + 观察池（资金动量权重最高——新闻可以热闹，资金不一定买单）</td></tr>"
             "<tr><td>产业链周期</td><td>主线的链处于周期哪个阶段？</td>"
             "<td>热区加成 / 衰退回避（复苏/扩张/过热/衰退 + 上中下游轮动）</td></tr>"
             "<tr><td>建仓质量</td><td>这笔交易值不值？</td>"
             "<td>精评分数 + 三种入场模板（回踩确认/收缩启动/趋势回撤）+ 止损锚点</td></tr>"
             "<tr><td>风控闸门</td><td>放行谁、放多少？</td>"
             "<td>五态行动（BUY/LIGHT/HOLD/WAIT/AVOID）+ 股数 + 交易卡片</td></tr></table>"
             "<div class='sub' style='margin-top:8px'>同构于机构三段式流程：放风控额度 → 定主线配置 "
             "→ 做交易执行；前后各补一段散户缺失的环节——前面的股票池扫描与情报处理"
             "（解决「看都没看到」），后面的落账结算与统计迭代（解决「做完就忘」）。</div></div>")

    # —— 刻意不赚的钱 ——
    P.append("<h2>这套系统刻意不赚的钱</h2>")
    P.append("<div class='grid g3'>"
             f"<div class='card'><b style='color:{RED}'>✕ 不赌财报</b>"
             "<div class='sub' style='margin-top:6px'>财报最大的不确定性是跳空：止损设得再漂亮，"
             "跳空也会直接越过止损价成交。事件前只做一件事——降不可控敞口；事件后等情绪释放"
             "再按结构跟随。</div></div>"
             f"<div class='card'><b style='color:{RED}'>✕ 不在市场不允许时硬做</b>"
             "<div class='sub' style='margin-top:6px'>负向共振时个股再强也可能被系统性抛售砸穿。"
             "系统宁可空仓，也不在这种日子里证明自己——波段最大的亏损来自「市场不允许时还硬做」。"
             "</div></div>"
             f"<div class='card'><b style='color:{RED}'>✕ 不用缺失或陈旧数据假装完整分析</b>"
             "<div class='sub' style='margin-top:6px'>本市场硬依赖行情不满足可用性或时效要求时，"
             "系统中止并说明原因；允许缺失的维度按其契约处理，并在报告中披露。"
             "</div></div></div>")

    # —— 风控 ——
    P.append("<h2>风控：用 R 记录计划风险</h2>")
    P.append(_idea("①", "仓位由止损距离反推，不是凭感觉",
                   "顺序与直觉相反：<b>先定「错了在哪里走」，再定「计划风险预算」，最后才算「买多少股」</b>。"
                   "止损越远就必须买得越少；想买得多，就必须找到更清晰、更近的结构位。"
                   "建仓时受单票仓位上限约束；跳空、滑点与佣金可能使实际亏损超过计划风险。"))
    P.append(_idea("②", "三类制度化离场，没有情绪余地",
                   "<b>结构止损</b>：跌破事先定义的结构位即走——亏 3% 不等于错，破关键位才等于错；"
                   "<b>时间止损</b>：入场 5–7 个交易日不推进或跑输主线，减仓或换股——资金效率是硬指标；"
                   "<b>盈利保护</b>：浮盈达两倍风险后上移止损，目标是减少利润回吐，不保证保本。"))
    P.append(_idea("③", "分批建仓与加仓铁律",
                   "首仓 40% / 二仓 40% / 三仓 20%（仅在环境与主线不降级时）。"
                   "<b>加仓的唯一合法理由是你变得更对</b>——趋势确认、结构推进；"
                   "摊平亏损单是波段最常见的爆仓路径，系统不允许。"))

    # —— AI 的真实角色 ——
    P.append("<h2>AI 的真实角色：语义归 LLM，数值归规则，闸门必须确定</h2>")
    P.append("<div class='card'><table>"
             "<tr><th>环节类型</th><th>驱动</th><th>理由</th></tr>"
             "<tr><td>实体消歧、情感、叙事兑现、链风险推断</td><td>LLM</td>"
             "<td>语义推理，规则做不了也不许做</td></tr>"
             "<tr><td>市场环境五维映射、板块资金动量、个股结构与动能、产业链周期</td>"
             "<td>规则</td><td>输入是数值、按映射表输出，零歧义、可回测——LLM 化反而引入不可回测性</td></tr>"
             "<tr><td>闸门、R 仓位反推、五态行动</td><td>规则</td>"
             "<td>风控必须确定性：同输入同输出，可回测可复现</td></tr>"
             "<tr><td>搜索调度、去重与格式校验</td><td>规则</td><td>确定性操作的保留区</td></tr></table>"
             "<div class='sub' style='margin-top:8px'>AI 不负责预测涨跌——它负责把该做的流程每天稳定"
             "做完：数据拉齐、信息结构化、规则落地、输出可执行的闸门权限与仓位上限。多 Agent 的意义"
             "是分工 + 交叉验证，消灭单点偏见与情绪化决策。每笔交易都能回答三句话：为什么买、错了在"
             "哪里走、对了怎么赚到。</div></div>")

    # —— 工程与诚实底座 ——
    P.append("<h2>工程与诚实底座（为什么敢公开直播）</h2>")
    P.append(_idea("①", "数据来源留痕 + 零基线纪律",
                   "行情按本轮配置与来源可用性选择通道，具体使用来源与覆盖情况逐轮披露；"
                   "允许降级不代表数据必然完整，每轮重新获取决策输入——"
                   "决策只能由「本轮真实数据 + 固定规则」产生，任何上轮残留进入决策都是污染。"))
    P.append(_idea("②", "工程红线：流程的机器级保险",
                   f"{len(STEP_REGISTRY)} 个注册环节每次运行逐一点名，缺一个即判系统性事故；"
                   "LLM 环节只有两个出口——"
                   "真实产出或透传兜底加留痕，<b>代码里不存在「LLM 失败改用规则冒充」的分支</b>；"
                   "系统的成熟标志不是永远有答案，而是永远说得清哪部分有答案、哪部分没有。"))
    P.append(_idea("③", "在统计面前保持诚实：落账结算 + WFA·DSR 迭代",
                   "没有结算的信号是口水，没有统计的胜率是感觉——每日信号自动落账，次日按与回测完全"
                   "相同的规则真实结算（策略验证中心）；滚动前推调参必须通过 DSR 多重检验校正，"
                   "不显著就保持理论默认参数：折内夏普 3.92 的「最优参数」折外期望转负，"
                   "系统正确地拒绝了它——这是量化系统最稀缺的美德。"))
    P.append(_idea("④", "双账公开验证：信号日记 + 小虎纯AI模拟盘",
                   "「策略验证中心」记录每一天的推荐信号及其结算结果（信号口径胜率）；"
                   "「小虎纯AI模拟盘」以台账记录的初始虚拟资金按 T+1 开盘价规则模拟交易"
                   "（账户口径净值），两者互为镜像、交叉验证。亏损交易日同样公示——"
                   "公开、可回溯，是我们对自身方法论的信心。"))

    # —— 结束语 ——
    P.append(f"<div class='card' style='margin-top:14px;text-align:center;border-color:{GOLD}55'>"
             "把每一次亏损都变成<b>流程可修复的事件</b>，而不是情绪可解释的故事；"
             "把每一次迭代都交给<b>统计检验</b>，而不是交给盘感。<br>"
             f"<b style='color:{GOLD};font-size:15px'>Discipline is the edge.</b></div>")

    # 只有实际输出目录内的可用 PDF 才能承诺相对路径下载。默认字符串渲染
    # （含严格工作流）没有附件授权，不从 cwd/仓库报告目录猜测或补拷贝。
    download = "<div class='sub'>本报告未附白皮书 PDF，当前没有可用的下载附件。</div>"
    if asset_dir is not None:
        try:
            pdf = report_assets.read_pdf_asset(asset_dir, report_assets.WHITEPAPER_FILENAME)
        except (OSError, ValueError):
            download = ("<div class='sub'>本报告未附可用的白皮书 PDF："
                        "文件缺失、不可读取或校验未通过。</div>")
        else:
            download = (
                f"<a href='{_esc(report_assets.WHITEPAPER_FILENAME)}' download "
                f"style='display:inline-block;background:{LIME};color:#1c2a10;font-weight:800;"
                f"padding:10px 28px;border-radius:10px;text-decoration:none;font-size:14.5px;"
                f"box-shadow:0 2px 14px #ccff0080'>⬇ 下载完整白皮书（PDF · {len(pdf):,} 字节）</a>")
    # —— 白皮书完整版及附件状态 ——
    P.append(f"<div class='card' style='margin-top:16px;border-color:#a8d400;text-align:center'>"
             "<div style='font-size:15px'><b>📕 《AI短线美股交易（1–15天波段版）白皮书》</b></div>"
             "<div class='sub' style='margin:6px 0 10px'>完整版 PDF · 16 章 + 4 附录：四层共振全章详解、"
             f"五环架构（白皮书原始口径 17 环节，现行内核 {len(STEP_REGISTRY)} 环节）、"
             "阈值与权限总表、交易卡片模板、每日看板读法</div>"
             f"{download}"
             "<div class='sub' style='margin-top:8px'>本白皮书为交易系统的方法论与工程说明，"
             "不构成任何证券买卖建议。</div></div>")
    return "".join(P)


# ---------------------------------------------------------------- 页面
def _scrollable_tables(markup: str) -> str:
    """Give generated tables a keyboard-accessible local viewport, preserving their cells."""
    return _re.sub(
        r"(<table\b.*?</table>)",
        r'<div class="table-scroll" role="region" aria-label="数据表格，可左右滚动查看完整内容" tabindex="0">\1</div>',
        markup, flags=_re.S,
    )


_CSS = f"""
* {{ box-sizing: border-box; }}
body {{ margin:0; background:{BG}; color:{FG};
  font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:28px 22px 60px; overflow-wrap:anywhere; }}
svg {{ max-width:100%; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
h2 {{ font-size:18px; margin:36px 0 14px; padding:0 0 8px 20px; position:relative;
  letter-spacing:0.5px;
  background:linear-gradient(90deg,#d4af3733,transparent 62%);
  border-bottom:2px solid; border-image:linear-gradient(90deg,#d4af37,#d4af3700) 1; }}
h2::before {{ content:''; position:absolute; left:2px; top:7px; width:9px; height:9px;
  background:linear-gradient(135deg,#ffe27a,#d4af37); transform:rotate(45deg);
  box-shadow:0 0 8px #d4af3780; }}
.sub {{ color:{MUTED}; font-size:13px; }}
.grid {{ display:grid; gap:14px; min-width:0; }}
.grid > * {{ min-width:0; }}
.g4 {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
.g3 {{ grid-template-columns:repeat(3,minmax(0,1fr)); }}
.g2 {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
@media (max-width:820px) {{ .g4,.g3,.g2 {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
.card {{ background:{CARD}; border:1px solid {BORDER}; border-radius:12px;
  padding:16px 18px; min-width:0; max-width:100%; }}
.kpi {{ font-size:26px; font-weight:700; margin-top:2px; }}
.badge {{ display:inline-block; padding:5px 16px; border-radius:999px;
  font-weight:700; font-size:15px; }}
.table-scroll {{ width:100%; max-width:100%; overflow-x:auto; overscroll-behavior-x:contain; }}
.table-scroll:focus-visible {{ outline:2px solid {GOLD}; outline-offset:3px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; overflow-wrap:normal; }}
th,td {{ padding:7px 9px; border-bottom:1px solid {BORDER}; text-align:left; }}
th {{ color:{MUTED}; font-weight:500; white-space:nowrap; }}
.lifecycle-steps {{ display:flex; align-items:center; margin:10px 0 6px; }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:7px 0; }}
.bar-label {{ width:92px; text-align:right; color:{MUTED}; font-size:12px; }}
.bar-track {{ flex:1; height:14px; background:#e6eecd; border-radius:7px;
  border:1px solid {BORDER}; overflow:hidden; }}
.bar-fill {{ height:100%; border-radius:7px; }}
.bar-val {{ width:88px; font-size:12px; }}
.tag {{ display:inline-block; padding:1px 9px; border-radius:999px; font-size:11px;
  border:1px solid {BORDER}; color:{MUTED}; margin-left:6px; }}
pre.card-block {{ background:#e6eecd; border:1px solid {BORDER}; border-radius:10px;
  padding:14px 16px; font:12.5px/1.7 "SF Mono",Consolas,monospace;
  white-space:pre-wrap; color:{FG}; }}
.ok {{ color:{GREEN}; }} .pass {{ color:{YELLOW}; }}
.footer {{ color:{MUTED}; font-size:12px; margin-top:36px; text-align:center; }}
.alert {{ background:#fdeaea; border:1px solid {RED}55; border-radius:10px;
  padding:10px 14px; margin:8px 0; }}
.tabbtn {{ background:#ffffff0d; color:#e8e6d2; border:1.5px solid #d4af3766;
  border-radius:12px; padding:12px 30px; font-size:16px; font-weight:600;
  cursor:pointer; letter-spacing:0.5px; transition:all .15s ease; }}
.tabbtn:hover {{ border-color:#ffd700; color:#ffd700; }}
.tabbtn.active {{ background:linear-gradient(135deg,#ffe27a,#ffd700 55%,#e6b400);
  color:#241a02; border-color:#ffd700;
  font-weight:800; box-shadow:0 2px 22px #ffd70080; }}
.subbtn {{ background:#fcfff2d9; color:{MUTED}; border:1px solid {BORDER};
  border-radius:999px; padding:7px 20px; font-size:13.5px; font-weight:600;
  cursor:pointer; transition:all .15s ease; backdrop-filter:blur(4px); }}
.subbtn:hover {{ border-color:{GOLD}88; color:{FG}; }}
.subbtn.active {{ background:#ccff0030; color:#4d7c0f; border-color:#a8d400;
  font-weight:700; }}
.research-overview,.research-security-panel {{ margin:16px 0; min-width:0; }}
.research-heading,.research-card-heading {{ display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }}
.research-heading h2 {{ margin:0; }}
.research-snapshot {{ border:1px solid {BORDER}; color:{MUTED}; background:{CARD}; padding:4px 12px; border-radius:999px; font-size:12px; }}
.research-identity {{ margin:9px 0; overflow-wrap:anywhere; }}
.research-axes {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.research-axis {{ padding:14px 16px; border:1px solid {BORDER}; border-radius:12px; background:{CARD}; min-width:0; }}
.research-axis strong {{ display:block; margin-top:4px; line-height:1.45; font-size:16px; }}
.research-notes {{ margin:10px 0; color:{MUTED}; font-size:12px; line-height:1.75; }}
.research-notes p {{ margin:4px 0; }}
.research-filters {{ display:flex; align-items:end; flex-wrap:wrap; gap:12px; margin:16px 0 8px; }}
.research-filters label {{ display:grid; gap:5px; min-width:0; flex:1; font-size:12px; font-weight:600; color:{MUTED}; }}
.research-filters input,.research-filters select,.research-reset {{ min-height:42px; border:1px solid {BORDER}; background:{CARD}; border-radius:9px; color:{FG}; font:inherit; padding:9px 12px; width:100%; }}
.research-filters input:hover,.research-filters select:hover,.research-reset:hover:enabled {{ border-color:{GOLD}; }}
.research-filters input:focus-visible,.research-filters select:focus-visible,.research-reset:focus-visible,.research-details summary:focus-visible {{ outline:2px solid {BLUE}; outline-offset:3px; }}
.research-reset {{ width:auto; font-size:13px; cursor:pointer; }}
.research-reset:active:enabled {{ background:#e1edc3; }}
.research-reset:disabled {{ opacity:.5; cursor:not-allowed; }}
.research-security-card,.research-source {{ background:{CARD}; border:1px solid {BORDER}; border-radius:12px; padding:18px; margin:12px 0; min-width:0; }}
.research-card-heading h3,.research-source h3 {{ margin:0; font-size:20px; }}
.research-state {{ display:inline-block; border:1px solid {BORDER}; padding:4px 12px; border-radius:999px; font-size:12px; font-weight:700; background:#f0f4e8; }}
.state-wait_evidence,.state-pending_research {{ color:#875600; border-color:#dec27d; background:#fff7dd; }}
.state-hold {{ color:#2354a0; border-color:#b4c7e8; background:#edf3ff; }}
.state-buy {{ color:#23602d; border-color:#aecb9e; background:#eaf7e5; }}
.research-fields {{ display:grid; grid-template-columns:minmax(100px,132px) minmax(0,1fr); gap:8px 16px; margin:16px 0; line-height:1.7; }}
.research-fields dt {{ color:{MUTED}; font-size:12px; }}
.research-fields dd {{ margin:0; min-width:0; overflow-wrap:anywhere; }}
.research-details {{ border-top:1px solid {BORDER}; padding-top:10px; }}
.research-details summary {{ cursor:pointer; padding:5px 0; font-size:13px; font-weight:600; }}
.research-details summary:hover {{ color:{GOLD}; }}
.research-details summary:active {{ color:#355b00; }}
.research-gates {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:10px; }}
.research-gates>div {{ padding:10px; background:#f1f6e5; border-radius:8px; min-width:0; }}
.research-gates span {{ margin-left:10px; font-size:12px; }}
.research-gates p {{ margin:4px 0 0; font-size:11px; overflow-wrap:anywhere; }}
.research-source-links {{ padding-top:10px; margin-top:10px; border-top:1px solid {BORDER}; font-size:12px; }}
.research-source a,.research-source-links a {{ color:#225baa; text-underline-offset:3px; }}
.research-source a:hover,.research-source-links a:hover {{ color:#174077; }}
.research-source:target {{ border-color:{BLUE}; box-shadow:0 0 0 2px #2563eb33; }}
.research-hash {{ font:11px/1.6 monospace; }}
.research-empty {{ border:1px dashed {BORDER}; padding:22px; border-radius:12px; color:{MUTED}; background:{CARD}; }}
[hidden] {{ display:none!important; }}
@media(max-width:820px) {{ .research-axes {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
@media(max-width:560px) {{
 .research-axes,.research-gates {{ grid-template-columns:minmax(0,1fr); }}
 .research-fields {{ grid-template-columns:minmax(0,1fr); gap:3px; }}
 .research-fields dd {{ margin-bottom:8px; }}
 .research-filters {{ display:grid; grid-template-columns:minmax(0,1fr); }}
 .research-reset {{ width:100%; }}
 .research-security-card,.research-source {{ padding:14px; }}
}}
.hero {{ position:relative; overflow:hidden; border:1px solid #d4af3755;
  border-radius:18px; margin:0 0 16px; background:#0a0a0c;
  box-shadow:0 8px 40px #0a0a0c40; }}
.hero-bg {{ position:absolute; inset:0; width:100%; height:100%; }}
.hero-art {{ position:absolute; right:0; top:0; height:100%; width:84%;
  object-fit:cover; object-position:center center; opacity:0.96;
  -webkit-mask-image:linear-gradient(to right,transparent 0%,#000 30%);
  mask-image:linear-gradient(to right,transparent 0%,#000 30%); }}
.hero-veil {{ position:absolute; inset:0;
  background:linear-gradient(90deg,#08080a 0%,#08080ac4 16%,#08080a3d 38%,transparent 54%);
  -webkit-mask-image:linear-gradient(to bottom,#000 68%,transparent 100%);
  mask-image:linear-gradient(to bottom,#000 68%,transparent 100%); }}
.hero-inner {{ position:relative; z-index:1; padding:58px 30px 50px; }}
.hero h1 {{ font-size:34px; letter-spacing:0.5px; color:#fdfaf0;
  text-shadow:0 0 24px #ffd70066,0 2px 6px #000000cc; }}
/* —— 调性组件：英文眉标 / 编辑级大标题 / 战绩条 / 跑马灯 —— */
.eyebrow {{ font-size:11px; letter-spacing:3.5px; font-weight:800; color:#d4af37;
  text-transform:uppercase; margin-bottom:6px; }}
.eyebrow-l {{ font-size:11px; letter-spacing:3.5px; font-weight:800; color:#a8842c;
  text-transform:uppercase; margin-bottom:4px; }}
.bigline {{ font-size:clamp(24px,3.6vw,38px); font-weight:900; line-height:1.3;
  color:#fdfaf0; text-shadow:0 0 26px #ffd7004d,0 2px 8px #000000d0; margin:2px 0 8px; }}
.hero-chips {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
.hchip {{ background:#08080a99; border:1px solid #d4af3744; border-radius:14px;
  padding:8px 14px; backdrop-filter:blur(4px); }}
.hchip .sub {{ color:#cbb26a; }}
.hchip .v {{ font-size:17px; font-weight:800; color:#fdfaf0; }}
.tape-wrap {{ overflow:hidden; border-top:1px solid #d4af3733;
  border-bottom:1px solid #d4af3733; background:#08080acc; margin:10px 0 0; }}
.tape {{ display:inline-block; white-space:nowrap; padding:8px 0;
  animation:tape 36s linear infinite; }}
.tape span {{ margin:0 18px; font-size:13px; color:#cbb26a; }}
.tape b {{ color:#ffd700; }}
@keyframes tape {{ from {{ transform:translateX(0); }} to {{ transform:translateX(-50%); }} }}
.hero .sub {{ color:#c9c6ac; }}
.card {{ transition:box-shadow .18s ease, transform .18s ease; }}
.card:hover {{ box-shadow:0 4px 20px #d4af3733; }}
@media (max-width:560px) {{
  .wrap {{ padding:16px 14px 48px; }}
  .g4,.g3,.g2 {{ grid-template-columns:minmax(0,1fr); }}
  .card {{ padding:14px 16px; }}
  .hero-inner {{ padding:36px 20px 32px; }}
  .hero h1 {{ font-size:28px; }}
  .tabbtn {{ padding:10px 18px; }}
  .table-scroll table {{ min-width:480px; }}
  .lifecycle-steps {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px 8px; }}
}}
"""

_TAB_JS = """
function countUp(id){
  var el = document.getElementById(id);
  if (!el || el.dataset.done) return;
  el.dataset.done = '1';
  var v = parseFloat(el.dataset.v || '0'), t0 = null;
  function step(ts){
    if (!t0) t0 = ts;
    var p = Math.min(1, (ts - t0) / 1000), e = 1 - Math.pow(1 - p, 3);
    el.textContent = '$' + Math.round(v * e).toLocaleString('en-US');
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
function showTab(name){
  ['daily','sim','philosophy'].forEach(function(t){
    var el = document.getElementById('tab-'+t);
    if (el) el.style.display = (t===name) ? '' : 'none';
    var btn = document.getElementById('btn-'+t);
    if (btn) btn.classList.toggle('active', t===name);
  });
  if (name === 'sim') countUp('simEquityKpi');
}
function showDaily(name){
  ['today','stock','history'].forEach(function(t){
    var el = document.getElementById('daily-pane-'+t);
    if (el) el.style.display = (t===name) ? '' : 'none';
    var btn = document.getElementById('daily-btn-'+t);
    if (btn) btn.classList.toggle('active', t===name);
  });
}
function showStock(id){
  document.querySelectorAll('[id^="stock-pane-"]').forEach(function(el){
    el.style.display = (el.id === 'stock-pane-'+id) ? '' : 'none';
  });
  document.querySelectorAll('[id^="stock-btn-"]').forEach(function(el){
    el.classList.toggle('active', el.id === 'stock-btn-'+id);
  });
}
function filterResearchSecurities(){
  var search = document.getElementById('research-security-search');
  var filter = document.getElementById('research-security-filter');
  if (!search || !filter) return;
  var query = search.value.trim().toLocaleLowerCase(), state = filter.value, shown = 0;
  var cards = document.querySelectorAll('[data-research-security]');
  cards.forEach(function(card){
    var matches = (state === 'all' || card.dataset.state === state) &&
      card.dataset.search.toLocaleLowerCase().includes(query);
    card.hidden = !matches;
    if(matches) shown++;
  });
  document.getElementById('research-security-count').textContent = '显示 ' + shown + ' / ' + cards.length + ' 只证券';
  document.getElementById('research-security-empty').hidden = shown !== 0;
  document.getElementById('research-security-reset').disabled = !query && state === 'all';
}
function resetResearchSecurities(){
  document.getElementById('research-security-search').value = '';
  document.getElementById('research-security-filter').value = 'all';
  filterResearchSecurities();
  document.getElementById('research-security-search').focus();
}
"""


def render_html(r: PipelineResult, journal_stats: dict | None = None,
                journal_entries: list[dict] | None = None,
                sim: dict | None = None, bench: dict | None = None,
                *, asset_dir: str | os.PathLike | None = None) -> str:
    """Render HTML; asset_dir is its actual destination directory, when known.

    No companion is read or promised by default. Callers supplying asset_dir
    must save this HTML there and preserve the declared PDF when distributing it.
    """
    raw = r.raw or {}
    research_status = report_research_status(raw, r.action)
    accounting_blocked = _execution_blocked(raw)
    accounting_note = _esc(_accounting_note(raw))
    market = _esc(_market_name(raw))
    currency = _esc(_market_currency(raw))
    display_action = ("WAIT_EVIDENCE" if research_status is not None
                      and research_status["axes"]["decision"]["state"] == "WAIT_EVIDENCE" else r.action)
    color, action_zh = _action_style(display_action)
    mrs = r.mrs
    P: list[str] = []
    P.append(f"<html><head><meta charset='utf-8'><meta name='viewport' "
             f"content='width=device-width,initial-scale=1'><title>"
             f"老虎全球资产管理 TGAM · {r.trade_date}</title><style>{_CSS}</style></head>")
    if HERO_ART_B64:
        # —— 一整张无缝大背景：猛虎图自页头贯穿向下，渐变融入嫩芽白正文（移动端加高取图）——
        P.append(f"<style>"
                 f"body {{ background:"
                 f"linear-gradient(to bottom, #f4fae400 0px, #f4fae400 330px, "
                 f"#f4fae4b8 640px, {BG} 940px) top center/100% 100% no-repeat, "
                 f"url('data:image/jpeg;base64,{HERO_ART_B64}') top center/100% auto "
                 f"no-repeat, {BG}; }}"
                 f".hero {{ background:transparent; border:none; box-shadow:none; }}"
                 f"@media (max-width:760px) {{ body {{ background:"
                 f"linear-gradient(to bottom, #f4fae400 0px, #f4fae400 430px, "
                 f"#f4fae4b8 600px, {BG} 820px) top center/100% 100% no-repeat, "
                 f"url('data:image/jpeg;base64,{HERO_ART_B64}') top center/auto 700px "
                 f"no-repeat, {BG}; }} }}</style>")
    P.append(f"<body><div class='wrap'>")

    # ---- Hero 头部（整站大背景之上的文字层：透明容器 + 装饰网格 + 左侧可读性纱幕）----
    P.append("<div class='hero'>")
    P.append(_hero_svg(overlay=bool(HERO_ART_B64)))
    if HERO_ART_B64:
        P.append("<div class='hero-veil'></div>")
    P.append("<div class='hero-inner'>")
    P.append("<div class='eyebrow'>TIGER GLOBAL ASSET MANAGEMENT · EVIDENCE, NOT OPINIONS.</div>")
    P.append(f"<h1>老虎全球资产管理 TGAM <span class='sub'>v3.1 · {r.trade_date}</span></h1>")
    P.append("<div class='bigline'>别人预测市场，我们执行纪律。</div>")
    P.append("<div class='sub' style='margin:2px 0'>AI 基金经理统筹的全自动化交易系统（美股 · A股 · 港股）"
             f" ｜ 本报告市场：<b style='color:#ffd700'>{market}</b></div>")
    # —— 战绩条：亮剑三数（今日状态 / 小虎净值 / 逐笔留痕）——
    sim_stats = (sim or {}).get("stats") or {}
    eq = sim_stats.get("equity")
    n_closed = (journal_stats or {}).get("closed", 0)
    win_r = (journal_stats or {}).get("win_rate")
    chips = [f"<div class='hchip'><div class='sub'>今日状态</div>"
             f"<div class='v' style='color:{color}'>{_esc(display_action)} · {_esc(action_zh)}</div></div>"]
    if eq and not accounting_blocked:
        chips.append(f"<div class='hchip'><div class='sub'>小虎模拟盘净值</div>"
                     f"<div class='v'>${eq:,.0f}</div></div>")
    if accounting_blocked:
        chips.append("<div class='hchip'><div class='sub'>交易记账</div>"
                     "<div class='v'>已暂停 · 币种边界受阻</div></div>")
    else:
        chips.append(f"<div class='hchip'><div class='sub'>逐笔留痕</div>"
                     f"<div class='v'>{n_closed} 笔已结算"
                     + (f" · 胜率 {win_r:.0%}" if n_closed and win_r is not None else "")
                     + "</div></div>")
    chips.append("<div class='hchip'><div class='sub'>首个 DSR 检查点</div>"
                 f"<div class='v' style='font-size:15px'>{_DSR_CHECKPOINT}</div></div>")
    P.append(f"<div class='hero-chips'>{''.join(chips)}</div>")

    # ---- 一级页签导航（决策日报 / 小虎纯AI模拟盘 / 核心交易理念）----
    P.append("<div style='display:flex;gap:12px;margin:20px 0 4px;flex-wrap:wrap'>"
             "<button class='tabbtn active' id='btn-daily' "
             "onclick='showTab(\"daily\")'>📈 决策日报</button>"
             "<button class='tabbtn' id='btn-sim' "
             "onclick='showTab(\"sim\")'>🤖 小虎纯AI模拟盘</button>"
             "<button class='tabbtn' id='btn-philosophy' "
             "onclick='showTab(\"philosophy\")'>🎓 核心交易理念</button></div>")
    # ---- 跑马灯：最新结算逐笔滚动（亏赚如实，翻倍重复以无缝循环）----
    tape_items = []
    for e in reversed((journal_entries or [])[-16:] if not accounting_blocked else []):
        tk = e.get("ticker", "")
        if e.get("status") == "closed" and e.get("r") is not None:
            col = "#16a34a" if e.get("win") else "#f87171"
            tape_items.append(f"<span>{e.get('exit_date', '')} <b>{tk}</b> "
                              f"<b style='color:{col}'>{e['r']:+.2f}R</b>"
                              f" · {_esc(e.get('note') or '已结算')}</span>")
        elif e.get("status") == "open":
            tape_items.append(f"<span>{e.get('date', '')} <b>{tk}</b> 信号放行 · 跟踪中</span>")
    if not tape_items:
        tape_items = ["<span>NO PREDICTION. PROCESS. · 信号逐笔留痕，亏赚如实公示</span>"]
    tape_html = "".join(tape_items)
    P.append(f"<div class='tape-wrap'><div class='tape'>{tape_html}{tape_html}</div></div>")
    P.append("</div></div>")  # /hero-inner /hero
    P.append("<div id='tab-daily'>")
    if research_status is not None:
        P.append(_research_overview(research_status))

    # ---- 决策日报门面：今日行动摘要 + 行情脉搏图 + 数据源公示（提升公信力）----
    P.append(f"<div class='card' style='margin:10px 0 14px;display:flex;gap:14px;"
             f"align-items:flex-start;justify-content:space-between;flex-wrap:wrap'>"
             f"<div style='flex:1;min-width:min(260px,100%)'>"
             f"<span class='badge' style='background:{color}22;color:{color};"
             f"border:1px solid {color}66'>{_esc(display_action)} · {action_zh}</span>")
    if mrs:
        P.append(f"<span class='tag'>市场环境 {mrs.mrs_star}/10（{mrs.regime}）</span>")
    if raw.get("universe_mode"):
        ss0 = raw.get("scan_stats", {})
        P.append(f"<span class='tag'>池 {ss0.get('universe_size', '?')} → "
                 f"精评 {ss0.get('selected', '?')}</span>")
    instructions = model_instructions(raw)
    company_note = company_evidence_note(raw) if instructions is not None else ""
    P.append(f"<div style='margin-top:8px;font-size:14.5px'>{_esc(r.market_view)}</div>")
    if company_note:
        P.append(f"<p class='sub'>{_esc(company_note)}</p>")
    audit_tables = research_audit_tables(raw)
    if audit_tables:
        P.append("<p class='sub'>阶段证据截至治理链校验。最终回执补齐日报生成和封存阶段。</p>")
        for title, columns, rows in audit_tables:
            P.append(f"<details><summary>{_esc(title)}</summary><table><thead><tr>"
                     + "".join(f"<th scope='col'>{_esc(col)}</th>" for col in columns)
                     + "</tr></thead><tbody>")
            for cells in rows:
                P.append("<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in cells) + "</tr>")
            P.append("</tbody></table></details>")
    P.append(f"</div><div style='flex:0 0 auto;opacity:0.95'>{_pulse_svg()}</div>")
    # —— 数据源公示：配置主源 + 本轮实际拉取血缘，真实可审计 ——
    prov_zh = _PROVIDER_ZH.get(r.provider, _esc(r.provider))
    lineage0 = raw.get("source_lineage", [])
    if lineage0:
        used0 = "、".join(sorted({_src_zh(src) for _, src in lineage0}))
        prov_txt = (f"{prov_zh} ｜ 本轮实际拉取：{_esc(used0)}"
                    f"（共 {len(lineage0)} 次，按实际来源留痕）")
    else:
        prov_txt = prov_zh
    P.append(f"<div class='sub' style='flex:1 0 100%;margin-top:0;padding-top:8px;"
             f"border-top:1px dashed {BORDER}'>数据源：<b style='color:{FG}'>"
             f"{prov_txt}</b><br>{_esc(_freshness_note(raw))}"
             f"<br>{_esc(_universe_note(raw))}"
             f"<br>行情报价币种：{currency}"
             + (f"<br>{accounting_note}" if accounting_note else "") + "</div></div>")

    # ---- 决策日报·二级栏目导航（今日决策报告 / 个股深度报告 / 策略验证）----
    P.append("<div style='display:flex;gap:8px;margin:10px 0 14px;flex-wrap:wrap;"
             "border-bottom:1px solid #d8e6b8; padding-bottom:12px'>"
             "<button class='subbtn active' id='daily-btn-today' "
             "onclick='showDaily(\"today\")'>今日决策报告</button>"
             "<button class='subbtn' id='daily-btn-stock' "
             "onclick='showDaily(\"stock\")'>个股深度报告</button>"
             "<button class='subbtn' id='daily-btn-history' "
             "onclick='showDaily(\"history\")'>📊 策略验证</button></div>")
    P.append("<div id='daily-pane-today'>")

    # ---- 四行看板 ----
    ss = raw.get("scan_stats", {})
    tech = raw.get("tech_signals", [])
    top_tech = max((s for s in tech if s.get("prosperity") is not None),
                   key=lambda s: s["prosperity"], default=None)
    P.append("<div class='grid g4'>"
             f"<div class='card'><div class='sub'>市场环境综合</div>"
             f"<div class='kpi'>{mrs.mrs_star if mrs else '—'}</div>"
             f"<div class='sub'>{mrs.regime if mrs else ''}</div></div>"
             f"<div class='card'><div class='sub'>主线板块</div>"
             f"<div class='kpi'>{' '.join(_ETF_ZH.get(s.etf, s.etf) for s in r.sectors if s.in_main_pool) or '—'}</div>"
             f"<div class='sub'>{' '.join(_ETF_ZH.get(s.etf, s.etf) for s in r.sectors if s.in_sub_pool)}</div></div>"
             f"<div class='card'><div class='sub'>最热科技赛道</div>"
             f"<div class='kpi'>{_chain_zh(top_tech['chain_id']) if top_tech else '—'}"
             f" <span class='sub'>{_prosperity_label(top_tech['prosperity']) if top_tech else ''}</span></div>"
             f"<div class='sub'>六条科技赛道中景气居首</div></div>"
             f"<div class='card'><div class='sub'>今日放行</div>"
             f"<div class='kpi'>{len(r.picks)} 只</div>"
             f"<div class='sub'>{' '.join(p.ticker for p in r.picks) or '无新放行信号'}</div>"
             + (f"<div class='sub'>{' · '.join(filter(None, (_name_zh(p.ticker) for p in r.picks)))}</div>"
                if any(_name_zh(p.ticker) for p in r.picks) else "")
             + "</div>"
             "</div>")

    # ---- 今日决策（基金经理视角：放行谁、为什么、怎么进怎么出）----
    P.append("<h2>今日决策</h2>")
    if research_status is not None:
        P.append("<div class='card'><b>逐证券判断与原文</b><p class='sub'>"
                 "每只证券分别列出证据是否充分、为何等待、是否待补查及原始纸面计划。"
                 "价格、股数和风险预算直接来自本轮执行决定。</p>"
                 "<button class='subbtn' onclick='showDaily(\"stock\")'>查看全部证券与来源 →</button></div>")
    elif instructions is not None:
        P.append(_model_instruction_cards(instructions))
    else:
        # —— 今日作战三句话：天气 / 方向 / 刀口（大白话翻译层）——
        cap = (mrs.position_cap if mrs and isinstance(mrs.position_cap, (tuple, list))
               else (0.0, mrs.position_cap if mrs else 0.0))
        if mrs:
            if mrs.mrs_star >= 7.5:
                w_line = f"天气不错。环境 {mrs.mrs_star}/10，系统给自己开了 {cap[1]:.0%} 弹药上限。"
            elif mrs.mrs_star >= 6.0:
                w_line = (f"能动手，但别上头。环境 {mrs.mrs_star}/10，"
                          f"弹药上限 {cap[1]:.0%}——不是每一天都值得满仓。")
            else:
                w_line = f"今天收着来。环境 {mrs.mrs_star}/10 没过关，空仓也是一种仓位。"
        else:
            w_line = "环境数据缺席，按纪律收着来。"
        main_secs = [s for s in r.sectors if s.in_main_pool]
        hot_chains = [c for c in r.chains if c.hot]
        d_parts = []
        if main_secs:
            d_parts.append("钱在往 " + "、".join(_ETF_ZH.get(s.etf, s.etf) for s in main_secs) + " 走")
        if hot_chains:
            d_parts.append("产业链里 " + "、".join(c.name for c in hot_chains[:2]) + " 最烫")
        d_line = "；".join(d_parts) + "。" if d_parts else "今天没有明确主线，方向不明就不硬做。"
        if r.picks:
            k_line = ("刀口看清了，放出 "
                      + "、".join(f"<b>{p.ticker}</b>" for p in r.picks)
                      + f" 这 {len(r.picks)} 枪——每笔都写清认错线，跌破就走，不商量。")
        elif accounting_blocked:
            k_line = "本轮仅保留研究；USD 交易记账暂停，原有账本保持原样。"
        else:
            k_line = "今日无新放行标的；既有持仓继续按出场规则管理。"
        P.append(f"<div class='card' style='border-color:#d4af3766;margin-bottom:12px'>"
                 f"<div class='eyebrow-l'>TODAY'S BATTLE PLAN · 今日作战三句话</div>"
                 f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin-top:4px'>"
                 f"<div style='flex:1;min-width:min(220px,100%)'><b>① 天气</b>"
                 f"<div class='sub' style='margin-top:2px'>{w_line}</div></div>"
                 f"<div style='flex:1;min-width:min(220px,100%)'><b>② 方向</b>"
                 f"<div class='sub' style='margin-top:2px'>{d_line}</div></div>"
                 f"<div style='flex:1;min-width:min(220px,100%)'><b>③ 刀口</b>"
                 f"<div class='sub' style='margin-top:2px'>{k_line}</div></div></div></div>")
    if r.picks and research_status is None:
        bull = THEME_ART_B64.get("tech", "")
        bull_img = (f"<img src='data:image/jpeg;base64,{bull}' width='96' height='96' "
                    f"alt='电路金牛插图' style='border-radius:14px;border:1.5px solid "
                    f"#d4af37;box-shadow:0 4px 20px #d4af3740'/>" if bull else "")
        P.append(f"<div style='display:flex;gap:14px;align-items:center;margin:-6px 0 10px'>"
                 f"<div class='sub' style='flex:1'>以下为决策摘要——"
                 "点上方二级栏目「个股深度报告」可查看每只股票的技术档案、"
                 "板块内部结构与赛道联动等完整深度分析。</div>"
                 f"{bull_img}</div>")
        for p in r.picks:
            P.append(_decision_card(r, p))
        # —— 战绩引流：今日决策 → 策略验证中心 ——
        n_cl = (journal_stats or {}).get("closed", 0)
        tot_r = (journal_stats or {}).get("total_r", 0)
        P.append(f"<div class='card' style='margin-top:12px;text-align:center;border-color:#d4af3766'>"
                 f"<div class='eyebrow-l'>TRACK RECORD · 战绩直播</div>"
                 f"<div style='font-size:15px'>至今已结算 <b>{n_cl}</b> 笔，累计 "
                 f"<b style='color:{GREEN if tot_r >= 0 else RED}'>{tot_r:+.2f}R</b>"
                 f"——每一笔都公开留痕，亏赚如实。</div>"
                 f"<button class='subbtn' style='margin-top:8px' "
                 f"onclick='showDaily(\"history\")'>查看完整验证 →</button></div>")
    elif not r.picks:
        ledger_note = ("本轮未更新交易账本，未执行持仓或待成交信号的模拟结算。"
                       if accounting_blocked else
                       "新的推荐信号与既有持仓分开管理，实际持仓和待成交记录见台账。")
        P.append(f"<div class='card'><b>今日无新放行标的</b><div class='sub' style='margin-top:6px'>"
                 f"{_esc(r.market_view)}；{ledger_note}</div></div>")
        # —— 开门条件（业务化，告诉投资者"等什么"）——
        if instructions is None and mrs:
            weak = []
            dims = mrs.dimensions
            for key, cond_zh in (
                ("macro", "本市场利率与流动性数据改善或恢复可用"),
                ("tech", "本市场基准指数趋势与关键均线结构改善"),
                ("flow", "市场广度修复（更多个股回到中期均线上方）"),
                ("sent", "本市场波动率与风险偏好数据改善或恢复可用"),
                ("micro", "期权微观结构数据恢复可用"),
            ):
                d = dims.get(key)
                sc = _get(d, "score") if d else None
                if sc is None or sc < (6.0 if key != "macro" else 4.0):
                    weak.append(cond_zh)
            if weak:
                P.append("<div class='card' style='margin-top:10px'><b>开门条件</b>"
                         "<div class='sub' style='margin-top:4px'>以下条件改善后，"
                         "将重新评估市场环境；是否放行仍取决于当轮全部闸门：</div>")
                for w_ in weak:
                    P.append(f"<div class='sub'>· {_esc(w_)}</div>")
                P.append("</div>")
        # —— 预备观察名单（环境开门后的第一梯队）——
        if instructions is None and r.watchlist:
            watch_note = ("个股质量与原币价位仅供研究；币种执行边界解除前不生成交易指令。"
                          if accounting_blocked else
                          "个股质量已评估完毕，等待市场环境开门——开门后按此优先级复核入场：")
            P.append("<div class='card' style='margin-top:10px'><b>预备观察名单</b>"
                     f"<div class='sub' style='margin-top:4px'>{watch_note}</div><table style='margin-top:6px'>"
                     "<tr><th>#</th><th>标的</th><th>日K收盘价</th><th>综合质量</th>"
                     "<th>入场形态</th><th>关键位</th><th>产业链</th></tr>")
            for i, c in enumerate(r.watchlist[:5], 1):
                nm = _name_zh(c.ticker)
                nm_html = f"<div class='sub'>{_esc(nm)}</div>" if nm else ""
                P.append(f"<tr><td>{i}</td><td><b>{c.ticker}</b>{nm_html}</td>"
                         f"<td>{c.price:.2f}</td>"
                         f"<td>{c.tss_final}/10</td>"
                         f"<td>{_TEMPLATE_ZH.get(c.entry_template, c.entry_template or '观察中')}</td>"
                         f"<td>{c.key_level:.2f}</td><td>{_chain_zh(c.chain_id)}</td></tr>")
            P.append("</table></div>")

    # ---- 市场环境体检 ----
    if mrs:
        P.append("<h2>一、市场环境体检</h2><div class='grid g2'>")
        cap = mrs.position_cap if isinstance(mrs.position_cap, (tuple, list)) else (0.0, mrs.position_cap)
        harmony = _harmony(mrs)
        P.append(f"<div class='card'>{_radar_svg(mrs.dimensions)}"
                 f"<div class='sub'>综合 {mrs.mrs_star}/10（{mrs.regime}）｜ {harmony}"
                 f" ｜ 建议总仓位 {cap[0]:.0%}–{cap[1]:.0%}</div></div>")
        P.append("<div class='card'><table>"
                 "<tr><th>体检维度</th><th>状态</th><th>解读</th></tr>")
        for name, d in mrs.dimensions.items():
            score = _get(d, "score")
            zh = _DIM_ZH.get(name, name)
            P.append(f"<tr><td>{zh}</td><td><b>{_health(score)}</b></td>"
                     f"<td class='sub'>{_esc(_dim_reading(name, score))}</td></tr>")
        P.append("</table></div></div>")
        # —— 五维仪表盘（黑金圆环，一眼看清谁在拖后腿）——
        P.append(f"<div style='margin-top:12px'>{_gauges_row_svg(mrs.dimensions)}</div>")

    # ---- 板块热度地图 ----
    P.append("<h2>二、板块热度地图</h2><div class='card'>")
    for s in r.sectors[:8]:
        c = GOLD if s.in_main_pool else (BLUE if s.in_sub_pool else "#84cc1688")
        badge = " 🔥主线" if s.in_main_pool else (" 次主线" if s.in_sub_pool else "")
        P.append(_bar_row(f"{_ETF_ZH.get(s.etf, s.etf)}{badge}", s.shs, 10.0, c,
                          extra=f"｜{s.breadth:.0f}% 成分股趋势向上"))
    P.append("<div class='sub' style='margin-top:8px'>读法：金色=当前最强主线，蓝色=次主线"
             "（热区链支撑），绿色=普通板块；括号内为本市场板块标识，可对照行情软件。</div></div>")

    # ---- 产业链景气周期 ----
    if r.chains:
        zh = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}
        P.append("<h2>三、产业链景气周期</h2><div class='card'>"
                 f"<div style='display:flex;gap:18px;align-items:center;flex-wrap:wrap'>"
                 f"<div style='flex:0 0 auto'>{_cycle_svg()}</div>"
                 f"<div class='sub' style='flex:1;min-width:min(240px,100%)'>产业链利润在"
                 f"<b>复苏 → 扩张 → 过热 → 衰退</b>四个阶段间循环轮动："
                 f"复苏与扩张期给持仓加成，过热期警惕补涨尾声，衰退期坚决回避——"
                 f"下表为{len(r.chains)}条产业链当前所处的阶段与领涨环节。</div></div>"
                 "<table>"
                 "<tr><th>产业链</th><th>景气</th><th>阶段</th><th>领涨环节</th>"
                 "<th>轮动</th><th>热区</th></tr>")
        for c in r.chains:
            P.append(f"<tr><td>{_esc(c.name)}</td><td><b>{c.ics}</b></td>"
                     f"<td>{c.stage}</td><td>{zh.get(c.leading_link, c.leading_link)}</td>"
                     f"<td>{_esc(c.rotation_signal)}</td>"
                     f"<td>{'🔥' if c.hot else ''}</td></tr>")
        P.append("</table></div>")

    # ---- 科技赛道景气扫描 ----
    if tech:
        ranked = sorted([s for s in tech if s.get("prosperity") is not None],
                        key=lambda s: s["prosperity"], reverse=True)
        P.append("<h2>四、科技赛道景气扫描</h2>")
        lead = ranked[0] if ranked else None
        if lead:
            lead_art = _theme_art_html(lead["chain_id"], width=110)
            P.append(f"<div class='card' style='margin-bottom:12px;border-color:{GOLD}66;"
                     f"display:flex;gap:16px;align-items:center;flex-wrap:wrap'>"
                     f"<div style='flex:1;min-width:min(260px,100%)'>"
                     f"<b>一句话结论：</b>六条科技赛道中，"
                     f"<b style='color:{GOLD}'>{_chain_zh(lead['chain_id'])}</b>"
                     f"当前景气居首（{_prosperity_label(lead['prosperity'])}），"
                     "该方向的个股在综合质量评分中获得额外支撑。</div>"
                     f"{lead_art}</div>")
        P.append("<div class='grid g2'>")
        for s in ranked:
            pros = s.get("prosperity")
            risk_txt, risk_color = _risk_label(s.get("risk_level"))
            support = s.get("bonus_hint", 1.0)
            tag = ("对板块评分构成支撑" if support > 1.02 else
                   "对板块评分构成拖累" if support < 0.98 else "对板块评分影响中性")
            lead_link = _LINK_ZH.get(s.get("leading_link"), "")
            P.append("<div class='card'>"
                     f"<div style='display:flex;justify-content:space-between;align-items:baseline'>"
                     f"<b style='font-size:15px'>{_chain_zh(s['chain_id'])}</b>"
                     f"<span class='tag' style='color:{GOLD};border-color:{GOLD}55'>"
                     f"{_prosperity_label(pros)}</span></div>")
            if pros is not None:
                P.append(_bar_row("景气度", pros, 10.0, GOLD,
                                  extra=f"｜{_prosperity_label(pros)}"))
            P.append(f"<div class='sub' style='margin:4px 0'>赛道风险："
                     f"<b style='color:{risk_color}'>{risk_txt}</b>"
                     f" ｜ {tag}{' ｜ 当前领涨环节：' + lead_link if lead_link else ''}</div>")
            P.append(_transmission_svg(s))
            for e in s.get("evidence", [])[:3]:
                P.append(f"<div class='sub'>· {_esc(_evidence_zh(e))}</div>")
            if s.get("degraded_components"):
                P.append("<div class='sub' style='margin-top:4px'>本轮 AI 语义辅助分析不可用，"
                         "该赛道按量价数据中性评估（已在系统自检留痕）。</div>")
            P.append("</div>")
        P.append("</div>")
        alerts = [a for s in tech for a in s.get("alerts", [])]
        if alerts:
            P.append("<h2>风险预警</h2>")
            for a in alerts[:8]:
                trans = "/".join(_LINK_ZH.get(t, t) for t in a.get("transmission", []))
                P.append(f"<div class='alert'>⚠️ [{a.get('severity'):.0f}/10 · "
                         f"{_esc(a.get('type'))}] {_esc(a.get('headline_zh'))}"
                         f"<span class='sub'>（影响环节: {trans or '—'}）</span></div>")

    # ---- 今日选股漏斗 ----
    if research_status is not None:
        counts = research_status["counts"]["securities"]
        P.append("<h2>五、今日选股漏斗</h2><div class='card'><p>逐证券状态按原记录展示；"
                 "未完成研究、缺证据与已完成判断分别计数。</p><div class='research-gates'>")
        for key, label in (("buy", "纸面买入计划"), ("qualified_hold", "合格观察"),
                           ("wait_evidence", "等待证据"), ("pending_research", "待补查"),
                           ("not_selected", "本轮未入选"), ("not_assessed", "尚未完成判断")):
            P.append(f"<div><strong>{label}</strong><span>{counts[key]} 只</span></div>")
        P.append("</div></div>")
    elif ss:
        rej = ss.get("rejected", {})
        total = ss.get("universe_size", 0) or 0
        passed, selected = ss.get("passed", 0), ss.get("selected", 0)
        n_pick = len(r.picks)
        rej_parts = [f"{_REJ_ZH.get(k, k)} {v} 只" for k, v in rej.items() if v]
        rej_txt = "、".join(rej_parts) if rej_parts else "本轮无标的触发基础门槛淘汰"

        def _funnel_row(label: str, n: int, note: str, color: str) -> str:
            w = max(6, round(n / total * 100)) if total else 0
            return (f"<div style='margin:8px 0'><div style='display:flex;"
                    f"justify-content:space-between;font-size:13px'>"
                    f"<span>{label}</span><b>{n} 只</b></div>"
                    f"<div style='width:{w}%;min-width:120px;background:{color};"
                    f"border-radius:6px;height:8px;margin:3px 0'></div>"
                    + (f"<div class='sub'>{note}</div>" if note else "") + "</div>")

        P.append("<h2>五、今日选股漏斗</h2><div class='card'>")
        if total:
            P.append(f"<div style='margin-bottom:6px'>"
                     f"{_funnel_svg(total, passed or 0, selected or 0, n_pick or 0)}</div>")
        P.append(_funnel_row("① 本轮扫描池", total,
                             _esc(_universe_note(raw)), "#d8e6b8"))
        P.append(_funnel_row("② 通过基础门槛", passed,
                             f"淘汰 {total - passed} 只：{rej_txt}", "#84cc1688"))
        P.append(_funnel_row("③ 进入深度精评", selected,
                             f"含主线板块额外补扫 {ss.get('mainline_boost', 0)} 只", BLUE))
        P.append(_funnel_row("④ 今日最终放行", n_pick,
                             ("三道关全部通过才放行" if n_pick else
                              "币种执行边界受阻；本轮仅保留研究，既有账本原样保留"
                              if accounting_blocked else
                              "本轮无新放行信号，既有持仓按出场规则管理"), GOLD))
        if total and n_pick is not None:
            rate = (1 - n_pick / total) * 100
            verdict = (f"从 {total} 只到 {n_pick} 只，淘汰率 {rate:.1f}%。"
                       + ("通过筛选仍需承担市场风险。"
                          if n_pick else "本轮未形成新的推荐信号；不据此推断台账已经空仓。"))
            P.append(f"<div style='margin-top:10px;padding-top:8px;border-top:1px dashed {BORDER}'>"
                     f"<b>漏斗结论：</b>{verdict}</div>")
        top10 = ss.get("top10") or []
        if top10:
            P.append("<div style='margin-top:12px'><b>扫描初排前十</b>"
                     "<div class='sub' style='margin:2px 0 6px'>本轮候选的扫描初排分"
                     "（含未放行标的，与后续综合精评分不同）：</div>")
            for i, item in enumerate(top10, 1):
                tk, sc = item[0], item[1]
                mark = " ✅ 今日放行" if any(p.ticker == tk for p in r.picks) else ""
                P.append(_bar_row(f"#{i} {tk}", sc, 10.0,
                                  GOLD if mark else "#d8e6b8", extra=f"/10{mark}"))
            P.append("</div>")
        P.append("</div>")
    P.append("<h2>六、个股精选清单（深度精评 Top 15）</h2><div class='card'><table>"
             "<tr><th>标的</th><th>日K收盘价</th><th>综合质量</th><th>价格结构</th><th>动能</th>"
             "<th>衍生品</th><th>入场形态</th><th>关键位</th><th>所属赛道</th></tr>")
    for c in r.watchlist[:15]:
        s_opt = c.s_options if c.s_options is not None else "—"
        nm = _name_zh(c.ticker)
        nm_html = f"<div class='sub'>{_esc(nm)}</div>" if nm else ""
        P.append(f"<tr><td><b>{c.ticker}</b>{nm_html}</td><td>{c.price:.2f}</td>"
                 f"<td>{c.tss_final}</td>"
                 f"<td>{c.s_structure}</td><td>{c.s_momentum}</td><td>{s_opt}</td>"
                 f"<td>{_TEMPLATE_ZH.get(c.entry_template, c.entry_template or '-')}</td>"
                 f"<td>{c.key_level:.2f}</td>"
                 f"<td>{_chain_zh(c.chain_id)}</td></tr>")
    P.append("</table>"
             "<div class='sub' style='margin-top:8px'>读法：综合质量满分 10 分；"
             "「入场形态」为系统识别的价格结构类型；「关键位」为计划入场参考价。"
             "清单含未放行标的——通过三道关的才会出现在今日决策中。</div></div>")

    # ---- 交易计划 ----
    P.append("<h2>七、交易计划与风控纪律</h2>")
    if r.picks:
        for p in r.picks:
            P.append(f"<pre class='card-block'>{_esc(p.card)}</pre>")
    else:
        trade_note = (accounting_note if accounting_blocked else
                      "今日无新放行标的；既有持仓继续按出场规则管理。")
        P.append(f"<div class='card sub'>{trade_note}</div>")

    # ---- 系统执行自检 ----
    redline = raw.get("redline", [])
    if redline:
        n_all = len(redline)
        n_pass = sum(1 for s in redline if s["status"] == "executed")
        n_skip = n_all - n_pass
        summary_txt = (f"{n_all} 个环节全部执行完成 ✅"
                       if not n_skip else
                       f"{n_pass}/{n_all} 个环节已执行；{n_skip} 个环节处于其他状态，"
                       "具体状态与原因逐项披露")
        P.append(f"<h2>八、系统执行自检</h2><div class='card'>{summary_txt}"
                 "<details style='margin-top:8px'><summary class='sub'>查看逐环节明细"
                 "（含耗时与备注）</summary><table style='margin-top:8px'>"
                 "<tr><th>环节</th><th>状态</th><th>耗时</th><th>备注</th></tr>")
        for s in redline:
            ok = s["status"] == "executed"
            status = {"executed": "已执行", "deferred": "待编排", "failed": "失败",
                      "skipped": "已跳过", "passthrough": "透传"}.get(s["status"], s["status"])
            mark = f"<span class='{'ok' if ok else 'pass'}'>{_esc(status)}</span>"
            P.append(f"<tr><td>{_STEP_ZH.get(s['step'], _esc(s['step']))}</td><td>{mark}</td>"
                     f"<td class='sub'>{s['ms']}ms</td>"
                     f"<td class='sub'>{_esc(s.get('note', '')[:70])}</td></tr>")
        P.append("</table></details></div>")

    # ---- 胜率追踪 ----
    P.append("<h2>九、胜率追踪（信号日记）</h2><div class='card'>")
    if accounting_blocked:
        P.append(f"<div class='sub'>{accounting_note} 本轮未更新或发布账本统计。</div>")
    elif journal_stats and journal_stats.get("closed", 0) > 0:
        P.append("<div class='grid g4'>"
                 f"<div><div class='sub'>已结算</div><div class='kpi'>{journal_stats['closed']}</div></div>"
                 f"<div><div class='sub'>总胜率</div><div class='kpi'>{journal_stats['win_rate']:.1%}</div></div>"
                 f"<div><div class='sub'>期望</div><div class='kpi'>{journal_stats['expectancy_r']}R</div></div>"
                 f"<div><div class='sub'>累计</div><div class='kpi'>{journal_stats['total_r']}R</div></div>"
                 "</div>")
        if journal_entries:
            svg = _r_curve_svg(journal_entries)
            if svg:
                P.append(f"<div style='margin-top:10px'>{svg}</div>")
    else:
        open_entries = [e for e in (journal_entries or []) if e.get("status") == "open"]
        held_n = sum(e.get("entry") is not None for e in open_entries)
        pending_n = len(open_entries) - held_n
        P.append(f"<div class='sub'>尚无已结算信号（持仓中 {held_n} 笔，待成交 {pending_n} 笔）——"
                 "账本逐日积累，每笔放行信号按出场规则自动结算。</div>")
    P.append("</div>")

    # ---- 披露 ----
    P.append("<h2>十、备注与数据缺失披露</h2><div class='card'>")
    for n in r.notes:
        P.append(f"<div class='sub'>· {_esc(n)}</div>")
    P.append("<div class='sub'>· 部分语义分析由 AI 模型完成；模型不可用时按系统纪律"
             "如实披露并留痕，绝不以简化规则冒充。</div>")
    P.append(f"<div class='sub'>· 数据覆盖 {raw.get('data_coverage', '—')} ｜ "
             f"耗时 {raw.get('elapsed_s', '—')}s ｜ 非预测、重流程、可审计，不构成投资建议。</div>")
    lineage = raw.get("source_lineage", [])
    if lineage:
        used = sorted({_src_zh(src) for _, src in lineage})
        P.append(f"<div class='sub'>· 行情来源血缘：本轮实际使用 <b>{_esc('、'.join(used))}</b>"
                 f"（{len(lineage)} 次拉取）</div>")
    P.append("</div>")

    P.append("</div>")  # /daily-pane-today

    # ---- 二级栏目：个股深度报告 ----
    P.append("<div id='daily-pane-stock' style='display:none'>")
    P.append(_stock_tab(r))
    P.append("</div>")

    # ---- 二级栏目：策略验证中心（成绩单/生命周期/归因实验室/全量台账）----
    P.append("<div id='daily-pane-history' style='display:none'>")
    if accounting_blocked:
        P.append(f"<div class='card sub'>{accounting_note} 本轮未更新或发布策略验证统计。</div>")
    else:
        P.append(_verify_tab(journal_entries, journal_stats, bench, sim))
    P.append("</div>")

    # ---- 首页底部：系统拓扑图（镇场子——数据到决策全链路一图收束）----
    P.append(f"<div style='margin-top:26px'>"
             f"<div class='sub' style='text-align:center;margin-bottom:8px;"
             f"letter-spacing:3px;font-weight:700;color:{GOLD}'>— 系统拓扑 —</div>"
             f"{_topology_svg()}</div>")

    P.append("</div>")  # /tab-daily

    # ---- 一级页签：小虎纯AI模拟盘（全 AI 掌控模拟盘，公开验证）----
    P.append("<div id='tab-sim' style='display:none'>")
    if accounting_blocked:
        P.append(f"<div class='card sub'>{accounting_note} 既有模拟盘台账原样保留。</div>")
    elif sim:
        P.append(_sim_tab(sim))
    else:
        P.append("<div class='card sub'>未提供模拟盘台账，初始资金、持仓与成交状态未知。</div>")
    P.append("</div>")  # /tab-sim

    # ---- 一级页签：核心交易理念 ----
    P.append("<div id='tab-philosophy' style='display:none'>")
    P.append(_philosophy_tab(raw, asset_dir=asset_dir))
    P.append("</div>")

    P.append(f"<div class='footer'>老虎全球资产管理（Tiger Global Asset Management）v3.1 · 生成于 {_esc(r.trade_date)}"
             f" · 汇聚顶级基金经理思想 × AI 能力的{market}交易系统</div>")
    P.append("</div>")
    P.append(f"<script>{_TAB_JS}</script>")
    P.append("</body></html>")
    return _scrollable_tables("".join(P))


# ---------------------------------------------------------------- CLI
def _result_from_json(path: str) -> PipelineResult:
    from .data_models import (ChainState, DimensionScore, MRSResult, SectorScore,
                               StockCandidate, TradePick)
    d = json.load(open(path, encoding="utf-8"))

    def _dim(x):
        return DimensionScore(name=x.get("name", ""), score=x.get("score"),
                              sub_scores=x.get("sub_scores", {}),
                              evidence=x.get("evidence", []), missing=x.get("missing", []))

    mrs_d = d.get("mrs") or {}
    mrs = None
    if mrs_d:
        cap = mrs_d.get("position_cap", (0.0, 0.0))
        if not isinstance(cap, (tuple, list)):
            cap = (0.0, float(cap))
        mrs = MRSResult(
            mrs_raw=mrs_d.get("mrs_raw", 0), delta=mrs_d.get("delta", 0),
            k=mrs_d.get("k", 1.0), mrs_star=mrs_d.get("mrs_star", 0),
            dimensions={k: _dim(v) for k, v in (mrs_d.get("dimensions") or {}).items()},
            regime=mrs_d.get("regime", ""), position_cap=(float(cap[0]), float(cap[1])),
            allow_new_positions=mrs_d.get("allow_new_positions", False),
            evidence=mrs_d.get("evidence", []),
        )
    sectors = [SectorScore(**{k: v for k, v in s.items()
                              if k in SectorScore.__dataclass_fields__})
               for s in d.get("sectors", [])]
    chains = [ChainState(**{k: v for k, v in c.items()
                            if k in ChainState.__dataclass_fields__})
              for c in d.get("chains", [])]
    watchlist = [StockCandidate(**{k: v for k, v in c.items()
                                   if k in StockCandidate.__dataclass_fields__})
                 for c in d.get("watchlist", [])]
    picks = [TradePick(**{k: v for k, v in p.items()
                          if k in TradePick.__dataclass_fields__})
             for p in d.get("picks", [])]
    return PipelineResult(
        trade_date=d.get("trade_date", ""), provider=d.get("provider", ""),
        mrs=mrs, sectors=sectors, chains=chains, watchlist=watchlist, picks=picks,
        action=d.get("action", ""), market_view=d.get("market_view", ""),
        notes=d.get("notes", []), raw=d.get("raw", {}),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="日报 JSON → 自包含 HTML 报告")
    ap.add_argument("result_json")
    ap.add_argument("--journal", default=None)
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    result = _result_from_json(args.result_json)
    stats, entries = None, None
    jpath = args.journal or os.path.join(os.path.dirname(args.result_json), "journal.json")
    if os.path.exists(jpath):
        from .journal import Journal
        j = Journal(jpath)
        stats = j.stats()
        entries = json.load(open(jpath, encoding="utf-8"))

    out = args.out or os.path.join(
        os.path.dirname(args.result_json),
        f"日报_{result.trade_date.replace('-', '')}.html")
    html_text = render_html(result, stats, entries, asset_dir=Path(out).parent)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_text)
    print(f"HTML 报告: {out}")


if __name__ == "__main__":
    main()
