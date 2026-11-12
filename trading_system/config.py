"""全局配置 — 与《核心指标及计算公式》波段版逐条对齐。

所有权重 / 阈值 / 档位表的 single source of truth。
修改必须同步更新 docs/UPGRADE_REPORT.md 的版本记录。
"""

# ============================================================
# MRS（市场共振评分）— 波段版权重（理论 §2.2）
# ============================================================
MRS_WEIGHTS = {
    "macro": 0.30,   # 宏观/利率水龙头
    "tech": 0.30,    # 技术结构
    "flow": 0.20,    # 资金广度
    "sent": 0.10,    # 情绪
    "micro": 0.10,   # 微观结构
}

# 一致性修正（理论 §2.3）：Δ = max(S_i) - min(S_i)，五维极差
# Δ < 4 → k=1.0；4 ≤ Δ ≤ 6 → k=0.8；Δ > 6 → k=0.5
def consistency_k(delta: float) -> float:
    if delta < 4.0:
        return 1.0
    if delta <= 6.0:
        return 0.8
    return 0.5

# MRS* → 总仓位上限（理论 §2.4）：(下限%, 上限%)
MRS_POSITION_CAP = [
    (8.0, 10.01, 0.70, 0.90),
    (6.0, 8.0, 0.40, 0.70),
    (4.0, 6.0, 0.10, 0.25),
    (0.0, 4.0, 0.00, 0.10),
]

MRS_GATE_BLOCK = 4.0          # MRS* < 4.0 禁止新开波段仓（除对冲白名单）
MRS_GATE_LIGHT = 6.0          # MRS* < 6.0 轻仓试探
EVENT_OVERNIGHT_DISCOUNT = (0.5, 0.7)  # 事件日前隔夜仓位折扣区间
# v6.0 事件风险管理（白皮书§11 + §4.5 三重现实折扣，机器执行）
EVENT_MACRO_DISCOUNT = 0.8      # CPI/FOMC/非农当日 Gross Cap ×0.8 + 加仓闸门延后30分钟
EVENT_OPEX_DISCOUNT = 0.9       # 期权到期周（每月第三个周五所在周）×0.9，偏模板A
EVENT_EARNINGS_SIZE = 0.5       # 标的 1-2 天内财报：仓位 ×0.5（或回避）
# v6.0 冲击折扣（Kill Switch，白皮书§4.5/§10.4）与流动性折扣阈值
SHOCK_SPY_1D_DROP = -0.04       # SPY 单日跌幅 ≤ -4%：逻辑断裂级冲击
SHOCK_VIX_1D_SPIKE = 0.30       # VIX 单日涨幅 ≥ 30%：恐慌冲击
LIQ_STRESS_ADV_RATIO = 0.65     # 池内 ADV20/ADV60 中位比 < 0.65：成交显著低于常态
LIQ_STRESS_DISCOUNT = 0.8       # 流动性折扣 Gross Cap ×0.8（理论区间 0.7-0.9 取中）

# ============================================================
# SHS（板块热度评分）— 波段版权重（理论 §3.2）
# ============================================================
SHS_WEIGHTS = {
    "macro": 0.25,   # 宏观适配（天时）
    "flow": 0.35,    # 资金动量（地利，最关键）
    "narr": 0.25,    # 叙事兑现/预期差（人和）
    "micro": 0.15,   # 微观结构催化（加速器）
}

SHS_MAIN_POOL = 7.5          # 主线入池：SHS ≥ 7.5 且广度 ≥ 60%
SHS_SUB_POOL = 7.0           # 次主线备选：7.0 ≤ SHS < 7.5
MAIN_POOL_MAX = 2            # 主线池最多 1-2 条（强制聚焦）
BREADTH_HEALTHY = 60.0       # 板块内部广度健康线（%）
BREADTH_NO_CHASE = 40.0      # 广度 < 40% 且仅权重拉动：不做追高

# 利率趋势 → 板块宏观适配固定映射表（理论 §2.1）
# TNX 20 日变化：Up(>+10bp) / Flat([-10,+10]bp) / Down(<-10bp)
SECTOR_MACRO_MAP = {
    # sector_etf: {Down, Flat, Up}
    "XLK":  {"Down": 9, "Flat": 6, "Up": 2},   # 长久期成长（软件/云）
    "SMH":  {"Down": 8, "Flat": 6, "Up": 3},   # 半导体
    "IWM":  {"Down": 7, "Flat": 6, "Up": 4},   # 小盘
    "XLF":  {"Down": 4, "Flat": 6, "Up": 8},   # 金融
    "XLU":  {"Down": 9, "Flat": 6, "Up": 2},   # 公用事业
    "XLRE": {"Down": 9, "Flat": 6, "Up": 2},   # REIT
    "XLE":  {"Down": 5, "Flat": 6, "Up": 6},   # 能源
    "XLP":  {"Down": 6, "Flat": 6, "Up": 5},   # 必选消费
    "XLV":  {"Down": 6, "Flat": 6, "Up": 5},   # 医疗
    "XLI":  {"Down": 6, "Flat": 6, "Up": 6},   # 工业
    "XLY":  {"Down": 7, "Flat": 6, "Up": 4},   # 可选消费
    "IBB":  {"Down": 8, "Flat": 6, "Up": 3},   # 生物科技
}

# 板块资金动量聚合：S_flow = round(0.45*A + 0.25*B + 0.30*C)
# A=RS斜率分位 B=R20 C=板块广度；叙事：A=EPS修正 B=指引 C=IV压缩；微观：A=CallOI B=Skew
SHS_FLOW_AGG = {"A": 0.45, "B": 0.25, "C": 0.30}
SHS_NARR_AGG = {"A": 0.45, "B": 0.35, "C": 0.20}
SHS_MICRO_AGG = {"A": 0.60, "B": 0.40}

# ============================================================
# TSS（交易建仓评分）— 波段版权重（理论 §4.1）
# ============================================================
TSS_WEIGHTS = {"structure": 0.40, "momentum": 0.40, "options": 0.20}
# 组件内聚合键统一为 A/B/C：structure=距关键位/突破回踩质量/流动性真空
TSS_STRUCTURE_AGG = {"A": 0.40, "B": 0.40, "C": 0.20}
# momentum=均线结构/波动收缩/ADX；options=CallOI分位/PC分位/IV分位
TSS_MOMENTUM_AGG = {"A": 0.40, "B": 0.35, "C": 0.25}
TSS_OPTIONS_AGG = {"A": 0.45, "B": 0.30, "C": 0.25}

# 三分数联动开仓标准（理论 §5）
OPEN_LONG = {"mrs": 6.0, "shs": 7.5, "tss": 7.2}
# v6.0：mrs_lo 收编 config（此前 5.5 硬编码在 risk_manager_agent）
LIGHT_PROBE = {"mrs_lo": 5.5, "tss": 7.8, "size_ratio": (0.30, 0.40)}  # 轻仓试错

# ============================================================
# 风控（理论 §6）
# ============================================================
RISK_R_PCT = 0.008           # 单笔最大风险 r = 账户净值 0.8%（0.5%-1.0% 区间中值）
# v6.0 组合层风控（白皮书§9/§10："仓位是风险预算"的机器执行）
MAX_CHAIN_RISK_PCT = 0.030   # 单条产业链累计风险 ≤ 账户 3%（防"N 个独立 R 实为同一个 R"）
ENFORCE_GROSS_CAP = True     # 计划总敞口不得超过 MRS* 仓位上限（截断多余 picks）
# v6.0 交易成本模型（白皮书§14 净口径调参）：单边 bps，作用于成交/结算价
COST_BPS = 10.0              # 单边 10bp（佣金+滑点合并估计，可按券商实际调整）
# v6.0 ATR 档位化时间止损（白皮书§10.3 "资金效率是硬指标"）：
# (ATR%上限, 时间止损交易日)——低波动给足 7 日，高波动 5 日快进快出
TIME_STOP_BY_ATR = [(0.04, 7), (0.07, 6), (9.9, 5)]
POSITION_FIRST = (0.40, 0.50)   # 首仓比例
POSITION_SECOND = (0.30, 0.40)  # 二仓比例
POSITION_THIRD = (0.10, 0.20)   # 三仓比例
PROFIT_PROTECT_R = 2.0          # 浮盈 ≥ 2R 启动盈利保护
TIME_STOP_DAYS = (5, 7)         # 时间止损窗口
MAX_SINGLE_POSITION_PCT = 0.20  # 单票仓位上限（行为风控）
MAX_PICKS_DEFAULT = 7

# ============================================================
# 全市场扫描器（Universe Scanner）硬过滤
# ============================================================
SCAN_MIN_PRICE = 5.0           # 最低价 $5（排除仙股）
SCAN_MIN_ADV_USD = 20_000_000  # 20 日平均成交额 ≥ $20M
SCAN_MIN_HISTORY_DAYS = 260    # 至少一年数据
SCAN_MAX_ATR_PCT = 0.12        # ATR14/价格 ≤ 12%（排除病态波动）
SCAN_TOP_N = 30                # 扫描产出候选数（进 TSS 精评）
SCAN_MAINLINE_BOOST = 5        # 主线定向补扫：主线池内额外带入的候选数

# full 模式（真实全市场 6000+ 只）两级拉取：先短历史预筛流动性，再拉全历史
FULL_PREFILTER_THRESHOLD = 1000   # 池规模超过此值启用预筛
FULL_PREFILTER_DAYS = 30          # 预筛用短历史长度
FULL_HEAVY_CAP = 800              # 预筛后进入重量级拉取的上限（按 ADV 排序）

# ============================================================
# TigerTest intelligence-first focus profile
# ============================================================
# One reviewed domain, two executable parent themes:
#   semiconductor_cycle -> semis
#   ai_infrastructure   -> ai_compute
# The focused universe is derived from these two chains. Raising liquidity
# requirements is a focus/quality choice, not a change to the white-paper
# minimums used by historical profiles.
TECH_FOCUS_UNIVERSE_MODE = "tech"
TECH_FOCUS_PARENT_CHAINS = ("semis", "ai_compute")
TECH_FOCUS_SUBCHAINS = ("memory", "logic", "foundry", "equipment", "ai_model", "ai_app")
TECH_FOCUS_SECTOR_ETFS = ("XLK", "SMH")
TECH_FOCUS_MIN_PRICE = 10.0
TECH_FOCUS_MIN_ADV_USD = 100_000_000
TECH_FOCUS_MIN_HISTORY_DAYS = 260
TECH_FOCUS_MAX_ATR_PCT = 0.12
TECH_FOCUS_FRONTIER_SIZE = 16
TECH_FOCUS_THEME_MINIMUM = 4
TECH_FOCUS_MAX_PICKS = 5
TECH_FOCUS_MAX_CONDITIONAL_WATCH = 8
TECH_FOCUS_MARKET_BATCH_SECONDS = 180
TECH_FOCUS_MARKET_CHUNK_SIZE = 20

# 扫描排序分（量化初排，非 TSS）
SCAN_RANK_WEIGHTS = {
    "rs_63": 0.30,     # 63 日相对 SPY 强度（一年分位）
    "rs_20": 0.25,     # 20 日相对强度
    "ma_align": 0.20,  # 均线多头结构
    "contraction": 0.15,  # 波动率收缩（蓄势）
    "near_high": 0.10, # 接近 52 周高点（强者恒强）
}

# ============================================================
# 产业链周期（ICS）— 新增大模块
# ============================================================
ICS_WEIGHTS = {
    "chain_rs": 0.30,       # 链整体相对强度
    "link_breadth": 0.25,   # 链内各环节广度（上中下游联动健康度）
    "rotation": 0.25,       # 环节轮动信号（领涨环节是否向中下游传导）
    "cycle_stage": 0.20,    # 周期阶段评分
}
ICS_HOT = 7.0               # ICS ≥ 7.0：产业链处于可交易热区
# 产业链加成：处于扩张期链条内的候选股 TSS 排序加成（乘性，≤1.15）
CHAIN_BONUS_MAX = 1.15

# ============================================================
# 数据与运行
# ============================================================
BENCHMARK = "SPY"
NEUTRAL_SCORE = 5.0          # 指标缺失时中性分（理论约定），并记录 evidence
PERCENTILE_WINDOW = 252      # 分位数滚动窗口（理论约定，约一年）
SECTOR_ETFS = ["XLK", "SMH", "XLF", "XLE", "XLV", "XLP", "XLY", "XLI", "XLU", "XLRE", "IBB", "IWM"]

DATA_PROVIDER = "yahoo"      # yahoo | stooq | demo
CACHE_DIR = "cache"
REPORTS_DIR = "reports"

# ============================================================
# v6.3 S2 决策层亮点注入
# ============================================================
# ---- 多空辩论证据层（桥水 AIA 辩论制）----
# 铁律：辩论只产出交易卡片"第六段"证据，绝不修改任何分数与闸门输出
# （MRS/SHS/ICS/TSS/TOS/五态行动一律不变）。回测路径禁用（无未来函数）。
DEBATE_ENABLED = True
DEBATE_MAX_PER_DAY = 3         # 每日辩论上限（灰区才辩论，防算力失控）
# 触发范围：①轻仓通道候选（MRS*∈[mrs_lo,6.0) 或仅次主线 且 TSS_final≥LIGHT_PROBE.tss，
# 即 rationale.standard=False 的放行标的）②HOLD 区高分标的（TSS_final≥下阈值）
DEBATE_HOLD_TSS_MIN = 7.8      # HOLD 区高分标的辩论触发线（与轻仓 TSS 门槛对齐）

# ---- 统计校准层（样本门槛，只进报告层不改闸门）----
# 属会计账白名单（与 journal 同级）：禁止进入决策输入（代码层隔离，见 calibration.py）
CALIBRATION_MIN_SAMPLES = 50   # 总样本 <50 只输出区间表述（Wilson 区间）
CALIBRATION_SAMPLES_PATH = "reports/calibration_samples.json"
CALIBRATION_JOURNAL_PATH = "reports/journal.json"
# TSS_final 分桶（与开仓门槛 7.2 / 轻仓门槛 7.8 对齐）
CALIBRATION_BUCKETS = [(7.2, 7.8), (7.8, 8.5), (8.5, 10.01)]

# ---- 小G 摩擦成本模型（防滑点断崖，v6.0 净口径的保守升级）----
# 按 20 日平均成交额（ADV）分档滑点：(ADV 下限$, 滑点 bp)——ADV 越小滑点越大
SIM_SLIPPAGE_BY_ADV = [
    (500_000_000, 5.0),
    (100_000_000, 10.0),
    (20_000_000, 25.0),
    (0.0, 50.0),
]
SIM_COMMISSION_BPS = 10.0      # 单边佣金 10bp（与 v6.0 回测净口径一致）

# ============================================================
# v6.3 S3 数据层（Tiger Data Fabric）— 可信度分级 / 分层 TTL / AH 主题
# ============================================================
# ---- 来源可信度分级（T0–T3，纯规则映射，D9：调度逻辑不引入 LLM）----
# T0 交易所/监管原文 > T1 主流财经媒体 > T2 聚合门户 > T3 社媒
# v3 FRED 宏观补充包（证据展示专用，**不参与评分**——评分输入的任何变更
# 都必须走 WFA/DSR 诚实迭代通道，不得直接并入闸门）。
FRED_MACRO_SERIES = {
    "DGS2": "美债 2 年期收益率",
    "T10Y2Y": "10Y-2Y 利差（倒挂预警）",
    "DFF": "联邦基金有效利率",
}

SOURCE_TIERS = {
    "edgar": "T0",              # SEC EDGAR 官方披露原文
    "federal_register": "T0",   # 美国联邦公报原文
    "patentsview": "T0",        # USPTO 官方专利库
    "kimi_search": "T1",        # agent-gw 全网搜索（白名单域内按 T1，其他域由 DOMAIN_TIERS 覆盖）
    "eastmoney_news": "T2",     # v3 东财中文资讯搜索（聚合门户）
    "sina_flash": "T2",         # v3 新浪 7×24 快讯（聚合门户）
    "google_news": "T1",        # Google News 主流媒体聚合入口（域名级再判定）
    "kimi_fetch": "T2",         # 按 URL 抓正文，默认聚合门户级（域名级再判定）
    "reddit": "T3",             # 社媒
    "demo": "T2",               # 离线演示源（不参与生产）
}
# 域名 → tier 覆盖表（后缀匹配）：同一源不同落地域按域名再分级
DOMAIN_TIERS = {
    # T0 监管/交易所原文
    "sec.gov": "T0", "federalregister.gov": "T0",
    "hkexnews.hk": "T0", "cninfo.com.cn": "T0",   # 港交所披露易/巨潮（S4 多市场启用）
    # T1 主流财经媒体白名单
    "reuters.com": "T1", "bloomberg.com": "T1", "wsj.com": "T1",
    "ft.com": "T1", "cnbc.com": "T1", "apnews.com": "T1", "nytimes.com": "T1",
    # T2 聚合门户
    "yahoo.com": "T2", "seekingalpha.com": "T2", "marketwatch.com": "T2",
    "benzinga.com": "T2", "eastmoney.com": "T2", "sina.com.cn": "T2",
    "10jqka.com.cn": "T2",
    # T3 社媒
    "reddit.com": "T3", "xueqiu.com": "T3", "x.com": "T3",
    "twitter.com": "T3", "stocktwits.com": "T3",
}
SOURCE_TIER_DEFAULT = "T2"      # 未标注来源按聚合门户级（保守）
# 交叉验证（规则层调度）：关键事件需 ≥2 个不同源佐证，且至少一个源 ≤ T2
CORROBORATION_MIN_SOURCES = 2
CORROBORATION_MAX_TIER = "T2"
# 关键事件类型（进入决策链路须交叉验证；LLM 清洗的事件标注归一到这些规范型）
KEY_EVENT_TYPES = ("earnings", "mna", "policy", "supply_chain", "capacity",
                   "macro", "litigation", "guidance")
KEY_EVENT_ALIASES = {
    "earnings": ("earnings", "财报", "业绩"),
    "mna": ("mna", "merger", "acquisition", "takeover", "并购", "收购"),
    "policy": ("policy", "regulation", "export_control", "tariff", "sanction",
               "政策", "监管", "管制", "关税"),
    "supply_chain": ("supply_chain", "supply", "供应链", "断供"),
    "capacity": ("capacity", "产能", "扩产"),
    "macro": ("macro", "fomc", "cpi", "jobs", "宏观", "非农"),
    "litigation": ("litigation", "lawsuit", "诉讼"),
    "guidance": ("guidance", "指引"),
}

# ---- 分层 TTL（缓存治理：行情秒级 / 新闻 15 分钟 / 公告 1 小时 / 宏观 1 天）----
TTL_TIERS = {
    "quote": 300,               # 行情/报价类：秒级
    "news": 900,                # 新闻舆情类
    "announcement": 3600,       # 公告/披露类（EDGAR/公报等纵向穿透）
    "macro": 86400,             # 宏观数据类
}
TTL_DEFAULT = 6 * 3600          # 未分类查询回退（与 v6.2 一致）

# ---- A/H 市场情报主题（S4 多市场铺路；默认关闭，启用仅打开配置开关）----
# 只含主题词与源路由配置，不新增抓取器——复用现有 6 源查询机制；
# eastmoney/sina 适配器为行情 provider，S4 落地搜索适配后并入路由。
AH_TOPICS_ENABLED = False
# S4：market ∈ AH_AUTO_MARKETS 时管线自动启用 AH 情报主题（等效于打开开关）
AH_AUTO_MARKETS = ("cn", "hk")
AH_TOPICS = {
    "cn_announcements": {"query": "沪深 上市公司 公告 重大事项 停复牌",
                         "sources": ["kimi_search", "google_news"]},
    "hkex_disclosure":  {"query": "港交所 披露易 公告 港股 盈警 配售",
                         "sources": ["kimi_search", "google_news"]},
    "southbound_flow":  {"query": "南向资金 港股通 净流入 持股",
                         "sources": ["kimi_search", "google_news"]},
    "cn_policy":        {"query": "A股 政策 证监会 央行 降准 监管",
                         "sources": ["kimi_search", "google_news"]},
    "hk_connect":       {"query": "港股通 标的调整 互联互通 恒生指数",
                         "sources": ["kimi_search", "google_news"]},
}

# ============================================================
# v6.3 S4 多市场（Market 抽象：US/CN/HK）
# 纪律：D1 框架不变——六层决策栈阈值结构不动，以下仅为"输入替换"参数；
#       D3 新市场全部参数集中在此披露；markets/*.py 只读不写。
# ============================================================

# ---- 市场基准组（MRS 硬依赖基准的市场映射）----
# index=大盘指数 rate=10Y利率 vol=波动率指数 vol_short=短期波动率（可空）
# CN/HK 免费源不可得维度：记缺失走"剔除再归一化"（D2，不钉中性分）
MARKET_BENCHMARKS = {
    "us": {"index": "SPY", "rate": "TNX", "vol": "VIX", "vol_short": "VIX9D",
           "index_label": "SPY", "rate_label": "TNX", "vol_label": "VIX"},
    "cn": {"index": "000300.SS", "rate": "CN10Y", "vol": "IVIX50", "vol_short": "",
           "index_label": "沪深300", "rate_label": "中债10Y", "vol_label": "50ETF波指iVIX"},
    "hk": {"index": "HSI.HK", "rate": "US10Y", "vol": "VHSI", "vol_short": "",
           "index_label": "恒生指数", "rate_label": "美债10Y/HIBOR", "vol_label": "恒指波指VHSI"},
}

# ---- 板块/广度代理符号（SHS 输入替换；免费源拿不到可靠广度时该维记缺失披露）----
CN_SECTOR_SYMBOLS = [   # 中证 800 行业指数系列（东财/新浪免费源可得性优先）
    "000928.SS", "000929.SS", "000930.SS", "000931.SS", "000932.SS",
    "000933.SS", "000934.SS", "000935.SS", "000936.SS", "000937.SS",
]
HK_SECTOR_SYMBOLS = [   # 恒生行业/主题指数占位（HSTECH 免费源可得性较好）
    "HSTECH.HK", "HSF.HK", "HSP.HK", "HSU.HK", "HSC.HK",
]
MARKET_SECTOR_SYMBOLS = {
    "us": SECTOR_ETFS,
    "cn": CN_SECTOR_SYMBOLS,
    "hk": HK_SECTOR_SYMBOLS,
}
# 板块覆盖率硬闸门是否诚实失败：US 保持现状（<2/3 中止）；CN/HK 免费源广度
# 不可靠时按缺失披露继续（D2：宁可缺失不可编造，绝不带病产出"看似完整"的 SHS）
MARKET_SECTOR_HARD_FAIL = {"us": True, "cn": False, "hk": False}

# ---- CN/HK 交易日历（规则实现：周末 + 内置节假日表，交易所公告后在此维护）----
CN_HOLIDAYS = {  # 沪深交易所休市日（2024-2027，国务院放假安排口径，遇调整在此改）
    # 2024
    "2024-01-01",
    "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
    "2024-04-04", "2024-04-05",
    "2024-05-01", "2024-05-02", "2024-05-03",
    "2024-06-10",
    "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    # 2025
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",
    "2025-04-04",
    "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026
    "2026-01-01",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-06",
    "2026-05-01",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    # 2027
    "2027-01-01",
    "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12",
    "2027-04-05",
    "2027-05-03",
    "2027-06-09",
    "2027-09-15",
    "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
}
HK_HOLIDAYS = {  # 港交所全日休市日（2024-2027）
    # 2024
    "2024-01-01", "2024-02-12", "2024-02-13",
    "2024-03-29", "2024-04-01", "2024-04-04",
    "2024-05-01", "2024-05-15", "2024-06-10",
    "2024-07-01", "2024-09-18", "2024-10-01", "2024-10-11",
    "2024-12-25", "2024-12-26",
    # 2025
    "2025-01-01", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-04-04", "2025-04-18", "2025-04-21",
    "2025-05-01", "2025-05-05",
    "2025-07-01", "2025-10-01", "2025-10-07", "2025-10-29",
    "2025-12-25", "2025-12-26",
    # 2026
    "2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-04-03", "2026-04-06",
    "2026-05-01", "2026-05-25", "2026-06-19",
    "2026-07-01", "2026-09-28", "2026-10-01", "2026-10-19",
    "2026-12-25",
    # 2027
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-02-10",
    "2027-03-26", "2027-03-29", "2027-04-05",
    "2027-05-03", "2027-05-13", "2027-06-09",
    "2027-07-01", "2027-09-16", "2027-10-01", "2027-10-07",
    "2027-12-27",
}
HK_HALF_DAYS = {  # 港交所半日市（仅上午交易）：平安夜/新年前夕/农历新年前夕
    "2024-02-09", "2024-12-24", "2024-12-31",
    "2025-01-28", "2025-12-24", "2025-12-31",
    "2026-02-16", "2026-12-24", "2026-12-31",
    "2027-02-05", "2027-12-24", "2027-12-31",
}

# ---- HK VCM 冷静期标的集合（市调机制适用证券：恒指+国指成分，交易所规则源，可维护）----
HK_VCM_SYMBOLS = [
    "0001.HK", "0002.HK", "0003.HK", "0005.HK", "0006.HK", "0011.HK", "0012.HK",
    "0016.HK", "0017.HK", "0027.HK", "0066.HK", "0083.HK", "0101.HK", "0135.HK",
    "0144.HK", "0151.HK", "0175.HK", "0241.HK", "0267.HK", "0288.HK", "0291.HK",
    "0293.HK", "0322.HK", "0386.HK", "0388.HK", "0669.HK", "0688.HK", "0700.HK",
    "0762.HK", "0823.HK", "0836.HK", "0857.HK", "0868.HK", "0881.HK", "0883.HK",
    "0939.HK", "0941.HK", "0960.HK", "0968.HK", "0981.HK", "1038.HK", "1044.HK",
    "1088.HK", "1093.HK", "1099.HK", "1109.HK", "1113.HK", "1177.HK", "1211.HK",
    "1299.HK", "1398.HK", "1810.HK", "1876.HK", "1928.HK", "1929.HK", "1997.HK",
    "2015.HK", "2020.HK", "2269.HK", "2313.HK", "2318.HK", "2319.HK", "2331.HK",
    "2382.HK", "2388.HK", "2628.HK", "2688.HK", "2899.HK", "3690.HK", "3692.HK",
    "3988.HK", "6618.HK", "6690.HK", "6862.HK", "9618.HK", "9633.HK", "9888.HK",
    "9961.HK", "9988.HK", "9999.HK",
]

# ---- CN 个股涨跌停规则（交易所规则源）----
CN_LIMIT_MAIN = 0.10       # 沪深主板 ±10%
CN_LIMIT_STAR_CHINEXT = 0.20  # 科创板(688)/创业板(300) ±20%
CN_LIMIT_ST = 0.05         # ST/*ST ±5%（名称含 "ST" 判定）
# US：无个股涨跌停，但有熔断 LULD 标记（披露用，不作追单拒绝）
# HK：无涨跌停，VCM 冷静期标的集合见 HK_VCM_SYMBOLS

# ---- 市场扫描硬过滤（按市场微调；US 沿用上方 SCAN_* 全局常量，不在此重复）----
MARKET_SCAN_FILTERS = {
    "cn": {"min_price": 2.0, "min_adv": 50_000_000, "currency": "CNY"},   # ≥2元，20日成交额≥5000万
    "hk": {"min_price": 0.5, "min_adv": 10_000_000, "currency": "HKD"},   # ≥0.5港元，20日成交额≥1000万
}

# ---- 轻仓通道毕业门槛（D7：新市场先轻仓，满 50 笔结算且 DSR 过关才开放标准通道）----
MARKET_GRAD_MIN_SETTLED = 50
MARKET_LIGHT_SIZE = (0.30, 0.40)     # 未达标市场强制轻仓系数区间（取中值执行）
# settled_trades/dsr_pass 为人工维护的毕业台账（信号日记结算统计 + WFA DSR 结论
# 达标后在此更新）；graduated 由门槛规则推导，不直接手改
MARKET_GRADUATION = {
    "us": {"settled_trades": 50, "dsr_pass": True},    # 成熟市场：已达标（现状行为）
    "cn": {"settled_trades": 0,  "dsr_pass": False},   # 默认未达标 → 强制轻仓
    "hk": {"settled_trades": 0,  "dsr_pass": False},
}

# ---- 市场基准硬失败策略：US 基准（SPY/TNX/VIX）缺失即诚实失败（现状行为）；
# CN/HK 指数缺失诚实失败，利率/波动率缺失走"剔除再归一化"披露 ----
MARKET_BENCHMARK_HARD_FAIL = {"us": True, "cn": False, "hk": False}

# ============================================================
# v6.4 S6 复盘闭环（诸葛团队：日度归因 / 周度体检 / 月度 WFA 提案）
# 纪律：复盘产物属会计账白名单（与 journal 同级），只进报告层，
#       绝不进入决策输入（D5 零基线）；只复盘流程不复盘运气（附录D）。
# ============================================================
REVIEW_PROPOSALS_DIR = "reports/review_proposals"   # 参数提案落盘目录
# D7 迭代诚实：DSR ≥ 0.95 且 OOS 期望为正且有推荐参数，提案才进入待审批；
# 否则自动 reject（不显著保持默认，绝不自动生效）
REVIEW_DSR_SIGNIFICANT = 0.95
# ---- 周度体检三档阈值（符合预期 / 关注 / 告警）----
REVIEW_WEEKLY_MIN_TRADES = 10     # 已结算样本 <10：样本积累中 → 关注档
REVIEW_WARN_EXPECTANCY_R = 0.10   # 期望 R 低于此值 → 关注
REVIEW_ALERT_EXPECTANCY_R = 0.0   # 期望 R ≤ 此值 → 告警
REVIEW_WARN_WIN_RATE = 0.40       # 胜率低于此值 → 关注
REVIEW_ALERT_WIN_RATE = 0.30      # 胜率低于此值 → 告警
